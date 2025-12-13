/*
 * src/port/pthread-win32.h
 */
#ifndef __PTHREAD_H
#define __PTHREAD_H

typedef ULONG pthread_key_t;
typedef DWORD pthread_t;
typedef void *pthread_attr_t;

typedef struct pthread_mutex_t
{
	/* initstate = 0: not initialized; 1: init done; 2: init in progress */
	LONG		initstate;
	CRITICAL_SECTION csection;
} pthread_mutex_t;

#define PTHREAD_MUTEX_INITIALIZER	{ 0 }

typedef int pthread_once_t;

pthread_t	pthread_self(void);

void		pthread_setspecific(pthread_key_t, void *);
void	   *pthread_getspecific(pthread_key_t);

int			pthread_mutex_init(pthread_mutex_t *, void *attr);
int			pthread_mutex_lock(pthread_mutex_t *);

/* blocking */
int			pthread_mutex_unlock(pthread_mutex_t *);

/* pthread_equal - compare thread IDs */
#define pthread_equal(t1, t2)	((t1) == (t2))

/* Thread creation/management - implemented in pthread-win32.c */
int			pthread_create(pthread_t *thread, pthread_attr_t *attr,
						   void *(*start_routine) (void *), void *arg);
int			pthread_join(pthread_t thread, void **retval);
void		pthread_exit(void *retval);

#endif
