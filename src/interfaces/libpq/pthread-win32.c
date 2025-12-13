/*-------------------------------------------------------------------------
*
* pthread-win32.c
*	 partial pthread implementation for win32
*
* Copyright (c) 2004-2025, PostgreSQL Global Development Group
* IDENTIFICATION
*	src/interfaces/libpq/pthread-win32.c
*
*-------------------------------------------------------------------------
*/

#include "postgres_fe.h"

#include <process.h>

#include "pthread-win32.h"

pthread_t
pthread_self(void)
{
	return GetCurrentThreadId();
}

void
pthread_setspecific(pthread_key_t key, void *val)
{
}

void *
pthread_getspecific(pthread_key_t key)
{
	return NULL;
}

int
pthread_mutex_init(pthread_mutex_t *mp, void *attr)
{
	mp->initstate = 0;
	return 0;
}

int
pthread_mutex_lock(pthread_mutex_t *mp)
{
	/* Initialize the csection if not already done */
	if (mp->initstate != 1)
	{
		LONG		istate;

		while ((istate = InterlockedExchange(&mp->initstate, 2)) == 2)
			Sleep(0);			/* wait, another thread is doing this */
		if (istate != 1)
			InitializeCriticalSection(&mp->csection);
		InterlockedExchange(&mp->initstate, 1);
	}
	EnterCriticalSection(&mp->csection);
	return 0;
}

int
pthread_mutex_unlock(pthread_mutex_t *mp)
{
	if (mp->initstate != 1)
		return EINVAL;
	LeaveCriticalSection(&mp->csection);
	return 0;
}

/*
 * Structure to pass arguments from pthread_create to the thread wrapper.
 */
typedef struct
{
	void	   *(*start_routine) (void *);
	void	   *arg;
} pthread_thread_args;

/*
 * Thread wrapper function that calls the actual start routine.
 * Uses __stdcall calling convention required by _beginthreadex.
 */
static unsigned __stdcall
pthread_thread_wrapper(void *argp)
{
	pthread_thread_args *args = (pthread_thread_args *) argp;
	void	   *(*start_routine) (void *) = args->start_routine;
	void	   *arg = args->arg;

	free(args);

	/* Call the actual thread function; ignore the return value */
	(void) start_routine(arg);

	return 0;
}

int
pthread_create(pthread_t *thread, pthread_attr_t *attr,
			   void *(*start_routine) (void *), void *arg)
{
	pthread_thread_args *args;
	uintptr_t	handle;
	unsigned	thread_id;

	args = (pthread_thread_args *) malloc(sizeof(pthread_thread_args));
	if (args == NULL)
		return ENOMEM;

	args->start_routine = start_routine;
	args->arg = arg;

	handle = _beginthreadex(NULL, 0, pthread_thread_wrapper, args, 0, &thread_id);
	if (handle == 0)
	{
		free(args);
		return errno;
	}

	/*
	 * Store the thread ID, not the handle.  We'll need to find the handle
	 * again in pthread_join using OpenThread.
	 */
	*thread = thread_id;

	/* We don't need the handle now; close it to avoid leaks */
	CloseHandle((HANDLE) handle);

	return 0;
}

int
pthread_join(pthread_t thread, void **retval)
{
	HANDLE		handle;
	DWORD		result;

	/* Get a handle for the thread */
	handle = OpenThread(SYNCHRONIZE, FALSE, thread);
	if (handle == NULL)
		return ESRCH;

	/* Wait for the thread to terminate */
	result = WaitForSingleObject(handle, INFINITE);
	CloseHandle(handle);

	if (result != WAIT_OBJECT_0)
		return EINVAL;

	/* We don't support getting the return value */
	if (retval != NULL)
		*retval = NULL;

	return 0;
}

void
pthread_exit(void *retval)
{
	_endthreadex(0);
}
