/*------------------------------------------------------------------------
 *
 * Query cancellation support for frontend code
 *
 * This module provides SIGINT/Ctrl-C handling for frontend tools that need
 * to cancel queries running on the server.  It combines three completely
 * independent mechanisms, any combination of which can be used by a caller:
 *
 * 1. Server cancel request -- Often what applications need. When a query is
 *    running, and the main thread is waiting for the result of that query in a
 *    blocking manner, we want SIGINT/Ctrl-C to cancel that query. This can be
 *    done by having the application call SetCancelConn() to register the
 *    connection that is running the query, prior to waiting for the result.
 *    When SIGINT/Ctrl-C is received a cancel request for this connection will
 *    then be sent to the server from a separate thread. That in turn will then
 *    (assuming a co-operating server) cause the server to cancel the query and
 *    send an error to the waiting client on the main thread. The cancel
 *    connection is a process-wide global, so only one connection can be the
 *    cancel target at a time. ResetCancelConn() can be used to unregister the
 *    connection again, preventing sending a cancel request if SIGINT/Ctrl-C is
 *    received after blocking wait has already completed.
 *
 * 2. CancelRequested flag -- A more involved but also much more flexible way
 *    of cancelling. A volatile sig_atomic_t CancelRequested flag is set to
 *    true whenever SIGINT is received. This means that the application code
 *    can fully control what it does with this flag. The primary usecase for
 *    this is when the application code is not blocked (indefinitely), but
 *    needs to take an action when Ctrl-C is pressed, such as break out of a
 *    long running loop.
 *
 * 3. Cancel callback -- The most complex way of handling a sigint. An optional
 *    function pointer registered via setup_cancel_handler().  If set, it is
 *    called directly from the signal handler, so it must be async-signal-safe.
 *    Writing async-signal-safe code is not easy, so this is only recommended
 *    as a last resort. psql uses this to longjmp back to the main loop when no
 *    query is active.
 *
 * Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * src/fe_utils/cancel.c
 *
 *------------------------------------------------------------------------
 */

#include "postgres_fe.h"

#include <signal.h>
#include <unistd.h>

#ifndef WIN32
#include <fcntl.h>
#endif

#ifdef WIN32
#include "pthread-win32.h"
#else
#include <pthread.h>
#endif

#include "common/connect.h"
#include "common/logging.h"
#include "fe_utils/cancel.h"
#include "fe_utils/string_utils.h"


/*
 * Write a simple string to stderr --- must be safe in a signal handler.
 * We ignore the write() result since there's not much we could do about it.
 * Certain compilers make that harder than it ought to be.
 */
#define write_stderr(str) \
	do { \
		const char *str_ = (str); \
		int		rc_; \
		rc_ = write(fileno(stderr), str_, strlen(str_)); \
		(void) rc_; \
	} while (0)


/*
 * Cancel connection that should be used to send cancel requests.
 */
static PGcancelConn *cancelConn = NULL;

/*
 * Mutex protecting cancelConn.  Held by SendCancelRequest() for the entire
 * duration of the cancel (including the blocking network I/O), so that
 * SetCancelConn()/ResetCancelConn() on the main thread will wait for the
 * cancel to finish before replacing or freeing cancelConn.
 */
static pthread_mutex_t cancelConnLock = PTHREAD_MUTEX_INITIALIZER;

/*
 * Predetermined localized error strings --- needed to avoid trying
 * to call gettext() from a signal handler.
 */
static const char *cancel_sent_msg = NULL;
static const char *cancel_not_sent_msg = NULL;

/*
 * CancelRequested is set when we receive SIGINT (or local equivalent).
 * There is no provision in this module for resetting it; but applications
 * might choose to clear it after successfully recovering from a cancel.
 * Note that there is no guarantee that we successfully sent a Cancel request,
 * or that the request will have any effect if we did send it.
 */
volatile sig_atomic_t CancelRequested = false;

/*
 * Additional callback for cancellations.
 */
static void (*cancel_callback) (void) = NULL;

#ifndef WIN32
/*
 * On Unix, the SIGINT signal handler cannot call PQcancelBlocking() directly
 * because it is not async-signal-safe.  Instead, we use a pipe to wake a
 * dedicated cancel thread: the signal handler writes a byte to the pipe, and
 * the cancel thread's blocking read() returns, triggering the actual cancel
 * request.
 */
static int	cancel_pipe[2] = {-1, -1};
static pthread_t cancel_thread;
#endif


/*
 * Send a cancel request to the connection, if one is set.
 *
 * Called from the cancel thread (Unix) or the console handler thread
 * (Windows), never from the signal handler itself.
 *
 * We hold cancelConnLock for the entire duration, so that the main thread's
 * SetCancelConn()/ResetCancelConn() will block until we're done.
 */
static void
SendCancelRequest(void)
{
	PGcancelConn *cc;

	pthread_mutex_lock(&cancelConnLock);
	cc = cancelConn;
	if (cc == NULL)
	{
		goto done;
	}

	write_stderr(cancel_sent_msg);

	if (!PQcancelBlocking(cc))
	{
		char	   *errmsg = PQcancelErrorMessage(cc);

		write_stderr(cancel_not_sent_msg);
		if (errmsg)
			write_stderr(errmsg);
	}
	/* Reset for possible reuse */
	PQcancelReset(cc);

done:

#ifndef WIN32
	{
		/*
		 * Drain any pending bytes from the cancel pipe. So that SIGINTs
		 * received while we were already cancelling don't cause the cancel
		 * thread to wake up again and cancel a subsequent query.
		 */
		char		buf[16];

		fcntl(cancel_pipe[0], F_SETFL, O_NONBLOCK);
		while (read(cancel_pipe[0], buf, sizeof(buf)) > 0)
		{
			/* loop until pipe is fully drained */
		}
		fcntl(cancel_pipe[0], F_SETFL, 0);
	}
#endif

	pthread_mutex_unlock(&cancelConnLock);
	return;
}


/*
 * Helper to replace cancelConn with a new value.
 *
 * Takes cancelConnLock, which also waits for any in-flight cancel request
 * to finish, since SendCancelRequest() holds the same lock while sending.
 */
static void
SetCancelConnInternal(PGcancelConn *newCancelConn)
{
	PGcancelConn *oldCancelConn;

	pthread_mutex_lock(&cancelConnLock);
	oldCancelConn = cancelConn;
	cancelConn = newCancelConn;
	pthread_mutex_unlock(&cancelConnLock);

	if (oldCancelConn != NULL)
		PQcancelFinish(oldCancelConn);
}

/*
 * SetCancelConn
 *
 * Set cancelConn to point to a cancel connection for the given database
 * connection. This creates a new PGcancelConn that can be used to send
 * cancel requests.
 */
void
SetCancelConn(PGconn *conn)
{
	SetCancelConnInternal(PQcancelCreate(conn));
}

/*
 * ResetCancelConn
 *
 * Clear cancelConn, preventing any pending cancel from being sent.
 * Waits for any in-flight cancel request to complete first.
 */
void
ResetCancelConn(void)
{
	SetCancelConnInternal(NULL);
}


#ifdef WIN32
/*
 * Console control handler for Windows.
 *
 * This runs in a separate thread created by the OS, so we can safely call
 * the blocking cancel API directly.
 */
static BOOL WINAPI
consoleHandler(DWORD dwCtrlType)
{
	if (dwCtrlType == CTRL_C_EVENT ||
		dwCtrlType == CTRL_BREAK_EVENT)
	{
		CancelRequested = true;

		if (cancel_callback != NULL)
			cancel_callback();

		SendCancelRequest();

		return TRUE;
	}
	else
		/* Return FALSE for any signals not being handled */
		return FALSE;
}

#else							/* !WIN32 */

/*
 * Cancel thread main function. Waits for the signal handler to write to the
 * pipe, then sends a cancel request.
 */
static void *
cancel_thread_main(void *arg)
{
	for (;;)
	{
		char		buf[16];
		ssize_t		rc;

		/* Wait for signal handler to wake us up (blocking read) */
		rc = read(cancel_pipe[0], buf, sizeof(buf));
		if (rc <= 0)
		{
			if (errno == EINTR)
				continue;
			/* Pipe closed or error - exit thread */
			break;
		}

		SendCancelRequest();
	}

	return NULL;
}

/*
 * Signal handler for SIGINT. Sets CancelRequested and wakes up the cancel
 * thread by writing to the pipe.
 */
static void
handle_sigint(SIGNAL_ARGS)
{
	int			save_errno = errno;
	char		c = 1;

	CancelRequested = true;

	if (cancel_callback != NULL)
		cancel_callback();

	/* Wake up the cancel thread - write() is async-signal-safe */
	if (cancel_pipe[1] >= 0)
	{
		int			rc = write(cancel_pipe[1], &c, 1);

		(void) rc;
	}

	errno = save_errno;
}

#endif							/* WIN32 */


/*
 * setup_cancel_handler
 *
 * Set up handler for SIGINT (Unix) or console events (Windows) to send a
 * cancel request to the server.
 *
 * The optional callback is invoked directly from the signal handler context
 * on every SIGINT (on Unix), so it must be async-signal-safe.
 */
void
setup_cancel_handler(void (*query_cancel_callback) (void))
{
	cancel_callback = query_cancel_callback;
	cancel_sent_msg = _("Sending cancel request\n");
	cancel_not_sent_msg = _("Could not send cancel request: ");

#ifdef WIN32
	SetConsoleCtrlHandler(consoleHandler, TRUE);
#else

	/*
	 * Create the pipe and cancel thread (see comment on cancel_pipe above).
	 */
	if (pipe(cancel_pipe) < 0)
	{
		pg_log_error("could not create pipe for cancel: %m");
		exit(1);
	}

	/*
	 * Make the write end non-blocking, so that the signal handler won't block
	 * if the pipe buffer is full (which is very unlikely in practice but
	 * possible in theory).
	 */
	fcntl(cancel_pipe[1], F_SETFL, O_NONBLOCK);

	/*
	 * Block SIGINT before creating the cancel thread, so that it inherits a
	 * signal mask with SIGINT blocked. This ensures SIGINT is always
	 * delivered to the main thread, which matters because some cancel
	 * callbacks (e.g. psql's) call siglongjmp() back to a sigsetjmp() on the
	 * main thread's stack.
	 */
	{
		sigset_t	sigint_sigset;
		int			rc;

		sigemptyset(&sigint_sigset);
		sigaddset(&sigint_sigset, SIGINT);
		pthread_sigmask(SIG_BLOCK, &sigint_sigset, NULL);

		rc = pthread_create(&cancel_thread, NULL, cancel_thread_main, NULL);

		pthread_sigmask(SIG_UNBLOCK, &sigint_sigset, NULL);

		if (rc != 0)
		{
			pg_log_error("could not create cancel thread: %s", strerror(rc));
			exit(1);
		}
	}

	pqsignal(SIGINT, handle_sigint);
#endif
}
