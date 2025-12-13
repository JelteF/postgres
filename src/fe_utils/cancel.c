/*------------------------------------------------------------------------
 *
 * Query cancellation support for frontend code
 *
 * This module provides SIGINT/Ctrl-C handling for frontend tools that need
 * to cancel queries or interrupt other operations. It combines four completely
 * independent mechanisms, any combination of which can be used by a caller:
 *
 * 1. Server cancel query request -- Often what applications need. When a query
 *    is running, and the main thread is waiting for the result of that query
 *    in a blocking manner, we want SIGINT/Ctrl-C to cancel that query. This
 *    can be done by having the application call SetCancelConn() to register
 *    the connection that is (or will be) running the query, prior to waiting
 *    for the result. When SIGINT/Ctrl-C is received a cancel request for this
 *    connection will then be sent to the server from a separate thread. That
 *    in turn will then (assuming a co-operating server) cause the server to
 *    cancel the query and send an error to the waiting client on the main
 *    thread. The cancel connection is a process-wide global, so only one
 *    connection can be the cancel target at a time. ResetCancelConn() can be
 *    used to unregister the connection again, preventing sending a cancel
 *    request if SIGINT/Ctrl-C is received after blocking wait has already
 *    completed.
 *
 * 2. CancelRequested flag -- A more involved but also much more flexible way
 *    of cancelling an operation. A volatile sig_atomic_t CancelRequested flag
 *    is set to true whenever SIGINT is received. This means that the
 *    application code can fully control what it does with this flag. The
 *    primary usecase for this is when the application code is not blocked
 *    (indefinitely), but needs to take an action when Ctrl-C is pressed, such
 *    as break out of a long running loop.
 *
 * 3. Thread handler callback -- An optional function pointer registered via
 *    setup_cancel_handler(). If set, this function is called from a separate
 *    thread when a cancel signal is received. If multiple signals are received
 *    in quick succession, the callback may be called only once. On Windows,
 *    this is called from the console handler thread. On Unix, this is called
 *    from the cancel thread that is woken by the signal handler. To ensure
 *    safe access to shared data, the cancel thread holds the cancel thread
 *    lock for the duration of the callback, so any other threads that need
 *    to access the same data should also acquire that lock using
 *    LockCancelThread()/UnlockCancelThread().
 *
 * 4. Signal handler callback -- The most complex way of canceling an
 *    operation, which is not supported on Windows. An optional signal_callback
 *    function pointer can be registered via setup_cancel_handler().  If set,
 *    it is called directly from the signal handler, so it must be
 *    async-signal-safe. Writing async-signal-safe code is not easy, so this is
 *    only recommended as a last resort. psql uses this to longjmp back to the
 *    main loop when no query is active. On Windows, this function is never
 *    called, since the console handler runs in a separate thread, not a signal
 *    handler.
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
 * Mutex held by the cancel thread for the duration of the cancel callback.
 * SetCancelConn()/ResetCancelConn() on the main thread take this lock too,
 * so they will wait for any in-flight cancel to finish before replacing or
 * freeing cancelConn.
 */
static pthread_mutex_t cancel_thread_lock = PTHREAD_MUTEX_INITIALIZER;

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
 * Signal handler callback, called directly from signal handler context.
 * Must be async-signal-safe.
 */
static void (*signal_callback_fn) (void) = NULL;

/*
 * Cancel thread callback, called from the cancel thread (Unix) or console
 * handler (Windows) when a cancel signal is received.  Returns true if the
 * signal was fully handled, false to let default processing continue (e.g.
 * ExitProcess on Windows).
 */
static bool (*thread_callback_fn) (void) = NULL;

#ifndef WIN32
/*
 * On Unix, the SIGINT signal handler cannot call PQcancelBlocking() directly
 * because it is not async-signal-safe.  Instead, we use a pipe to wake a
 * dedicated cancel thread: the signal handler writes a byte to the pipe, and
 * the cancel thread's blocking read() returns, triggering the actual cancel
 * request.
 */
static int	cancel_pipe[2] = {-1, -1};
#endif


/*
 * Send a cancel request to the connection, if one is set.
 *
 * Called from the cancel thread (Unix) or the console handler thread
 * (Windows), never from the signal handler itself.  The caller is
 * responsible for holding cancel_thread_lock.
 */
static void
SendCancelRequest(void)
{
	PGcancelConn *cc;

	cc = cancelConn;
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
}


/*
 * Helper to replace cancelConn with a new value.
 *
 * Takes cancel_thread_lock, which also waits for any in-flight cancel
 * callback to finish, since the cancel thread holds the same lock.
 */
static void
SetCancelConnInternal(PGcancelConn *newCancelConn)
{
	PGcancelConn *oldCancelConn;

	LockCancelThread();
	oldCancelConn = cancelConn;
	cancelConn = newCancelConn;
	UnlockCancelThread();

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


/*
 * LockCancelThread / UnlockCancelThread
 *
 * Acquire or release cancel_thread_lock.  External callers (e.g. pg_dump)
 * use these to protect shared data that the cancel-thread callback also
 * accesses, without exposing the mutex directly.
 */
void
LockCancelThread(void)
{
	pthread_mutex_lock(&cancel_thread_lock);
}

void
UnlockCancelThread(void)
{
	pthread_mutex_unlock(&cancel_thread_lock);
}

#ifndef WIN32
/*
 * ResetCancelAfterFork
 *
 * Reset cancel module state after fork(). Threads don't survive fork(), so the
 * cancel thread and its pipe are gone. The mutex may have been held by the
 * cancel thread at fork time, so we must reinitialize it rather than trying to
 * unlock it.  cancelConn is NULLed without freeing because the parent process
 * owns the underlying object.  The SIGINT handler is reset to SIG_DFL so that
 * a signal arriving before setup_cancel_handler() is called again doesn't try
 * to write to the closed pipe.
 *
 * The child will set up a fresh cancel thread when it later calls
 * setup_cancel_handler().
 */
void
ResetCancelAfterFork(void)
{
	close(cancel_pipe[0]);
	close(cancel_pipe[1]);
	cancel_pipe[0] = cancel_pipe[1] = -1;

	pthread_mutex_init(&cancel_thread_lock, NULL);

	cancelConn = NULL;
	CancelRequested = false;

	pqsignal(SIGINT, PG_SIG_DFL);
}
#endif

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
		BOOL		result = TRUE;

		CancelRequested = true;

		LockCancelThread();

		SendCancelRequest();

		if (thread_callback_fn != NULL)
			result = thread_callback_fn();

		UnlockCancelThread();

		return result;
	}
	else
		/* Return FALSE for any signals not being handled */
		return FALSE;
}

#else							/* !WIN32 */

/*
 * Signal handler that setup_cancel_handler configures for SIGINT. Exposed so
 * other signals than SIGINT can use it if desired.
 */
void
CancelSignalHandler(SIGNAL_ARGS)
{
	int			save_errno = errno;

	CancelRequested = true;

	if (signal_callback_fn != NULL)
		signal_callback_fn();

	/* Wake up the cancel thread */
	if (cancel_pipe[1] >= 0)
	{
		char		c = 1;
		int			rc = write(cancel_pipe[1], &c, 1);

		(void) rc;
	}

	errno = save_errno;
}

/*
 * Thread main function for create_cancel_thread.  Waits for the signal
 * handler to write a byte to the pipe, then calls the cancel callback.
 */
static void *
cancel_thread_loop(void *arg)
{
	for (;;)
	{
		char		buf[16];
		ssize_t		rc;

		rc = read(cancel_pipe[0], buf, sizeof(buf));
		if (rc <= 0)
		{
			if (errno == EINTR)
				continue;
			/* Pipe closed or error - exit thread */
			break;
		}

		LockCancelThread();

		SendCancelRequest();

		if (thread_callback_fn != NULL)
			thread_callback_fn();

		/*
		 * Drain any pending bytes from the cancel pipe, so that signals
		 * received while we were already handling a cancel don't cause us to
		 * wake up again and cancel a subsequent query.
		 */
		fcntl(cancel_pipe[0], F_SETFL, O_NONBLOCK);
		while (read(cancel_pipe[0], buf, sizeof(buf)) > 0)
			;					/* loop until pipe is fully drained */
		fcntl(cancel_pipe[0], F_SETFL, 0);

		UnlockCancelThread();
	}

	return NULL;
}

/*
 * create_cancel_thread
 *
 * Create a dedicated thread and associated pipe for async-signal-safe cancel
 * handling.  The pipe allows signal handlers (which cannot safely call complex
 * functions) to wake up the thread by writing a byte.
 *
 * The write end of the pipe is set non-blocking so signal handlers never
 * block.  The thread is created with all signals blocked so that signals are
 * always delivered to the main thread.  The thread runs until process exit.
 * No handle is returned because currently no callers need to join it.
 */
static void
create_cancel_thread(void)
{
	sigset_t	save_set;
	sigset_t	block_set;
	pthread_t	thread;
	int			rc;

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
	 * Block all signals before creating the cancel thread, so that it
	 * inherits a signal mask with all signals blocked.  This ensures signals
	 * are always delivered to the main thread, which matters because some
	 * signal_callback functions call siglongjmp() back to a sigsetjmp() on
	 * the main thread's stack, specifically the psql_cancel_callback
	 * function.
	 */
	sigfillset(&block_set);
	pthread_sigmask(SIG_BLOCK, &block_set, &save_set);

	rc = pthread_create(&thread, NULL, cancel_thread_loop, NULL);

	pthread_sigmask(SIG_SETMASK, &save_set, NULL);

	if (rc != 0)
	{
		pg_log_error("could not create cancel thread: %s", strerror(rc));
		exit(1);
	}

	pthread_detach(thread);
}

#endif							/* !WIN32 */


/*
 * setup_cancel_handler
 *
 * Set up signal handling for SIGINT (Unix) or console events (Windows) to
 * perform cancel actions.
 *
 * signal_callback is invoked directly from the signal handler context on
 * every SIGINT (on Unix), so it must be async-signal-safe.  Can be NULL.
 * On Windows, signal handlers don't exist (the console handler runs in a
 * separate thread), so signal_callback must be NULL.
 *
 * thread_callback is invoked from a dedicated cancel thread (Unix) or the
 * console handler thread (Windows) when a signal is received.  It should
 * return true if the signal was fully handled, or false to allow default
 * processing to continue (relevant on Windows for ExitProcess).  If NULL,
 * the default SendCancelRequest is used.
 */
void
setup_cancel_handler(void (*signal_callback) (void),
					 bool (*thread_callback) (void))
{
#ifdef WIN32
	Assert(signal_callback == NULL);
#endif

	signal_callback_fn = signal_callback;
	thread_callback_fn = thread_callback;
	cancel_sent_msg = _("Sending cancel request\n");
	cancel_not_sent_msg = _("Could not send cancel request: ");

#ifdef WIN32
	SetConsoleCtrlHandler(consoleHandler, TRUE);
#else
	create_cancel_thread();
	pqsignal(SIGINT, CancelSignalHandler);
#endif
}
