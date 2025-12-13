/*------------------------------------------------------------------------
 *
 * Query cancellation support for frontend code
 *
 * Assorted utility functions to control query cancellation with signal
 * handler for SIGINT.
 *
 *
 * Portions Copyright (c) 1996-2025, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * src/fe_utils/cancel.c
 *
 *------------------------------------------------------------------------
 */

#include "postgres_fe.h"

#include <signal.h>
#include <unistd.h>

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
 * Generation counter for cancelConn. Incremented each time cancelConn is
 * changed. Used to detect if cancelConn was replaced while we were using it.
 */
static uint64 cancelConnGeneration = 0;

/*
 * Mutex protecting cancelConn and cancelConnGeneration.
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
 * On Unix, we use a self-pipe to wake up the cancel thread from the signal
 * handler, since pthread_cond_signal() is not async-signal-safe.
 */
static int	cancel_pipe[2] = {-1, -1};
static pthread_t cancel_thread;
static volatile bool cancel_thread_running = false;
#endif


/*
 * Send a cancel request to the connection, if one is set.
 */
static void
SendCancelRequest(void)
{
	PGcancelConn *cc;
	uint64		generation;
	bool		putConnectionBack = false;

	/*
	 * We take the cancel connection out of the global. This ensures that
	 * ResetCancelConn or SetCancelConn won't free it while we're using it.
	 */
	pthread_mutex_lock(&cancelConnLock);
	cc = cancelConn;
	generation = cancelConnGeneration;
	cancelConn = NULL;
	pthread_mutex_unlock(&cancelConnLock);

	if (cc == NULL)
		return;

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

	/*
	 * Put the cancel connection back if it wasn't replaced while we were
	 * using it.
	 */
	pthread_mutex_lock(&cancelConnLock);
	if (cancelConnGeneration == generation)
	{
		/* Generation unchanged, put it back for reuse */
		cancelConn = cc;
		putConnectionBack = true;
	}
	pthread_mutex_unlock(&cancelConnLock);

	/* If it was replaced, we free it, because we were the last user */
	if (!putConnectionBack)
		PQcancelFinish(cc);
}


/*
 * Helper to replace cancelConn with a new value.
 */
static void
SetCancelConnInternal(PGcancelConn *newCancelConn)
{
	PGcancelConn *oldCancelConn;

	pthread_mutex_lock(&cancelConnLock);
	oldCancelConn = cancelConn;
	cancelConn = newCancelConn;
	cancelConnGeneration++;
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

		/* Wait for signal handler to wake us up */
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
		(void) write(cancel_pipe[1], &c, 1);

	errno = save_errno;
}

#endif							/* WIN32 */


/*
 * setup_cancel_handler
 *
 * Set up handler for SIGINT (Unix) or console events (Windows) to send a
 * cancel request to the server. Also sets up the infrastructure to send
 * cancel requests asynchronously.
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
	 * Set up the self-pipe for communication between signal handler and
	 * cancel thread. We use a pipe because write() is async-signal-safe.
	 */
	if (cancel_pipe[0] < 0)
	{
		if (pipe(cancel_pipe) < 0)
		{
			pg_log_error("could not create pipe for cancel: %m");
			exit(1);
		}
	}

	/* Start the cancel thread if not already running */
	if (!cancel_thread_running)
	{
		int			rc;

		rc = pthread_create(&cancel_thread, NULL, cancel_thread_main, NULL);
		if (rc != 0)
		{
			pg_log_error("could not create cancel thread: %s", strerror(rc));
			exit(1);
		}
		cancel_thread_running = true;
	}

	pqsignal(SIGINT, handle_sigint);
#endif
}
