/*-------------------------------------------------------------------------
 *
 * hsearch.h
 *	  exported definitions for utils/hash/dynahash.c; see notes therein
 *
 *
 * Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * src/include/utils/hsearch.h
 *
 *-------------------------------------------------------------------------
 */
#ifndef HSEARCH_H
#define HSEARCH_H


/* Hash table control struct is an opaque type known only within dynahash.c */
typedef struct HTAB HTAB;

/*
 * Hash functions must have this signature.
 */
typedef uint32 (*HashValueFunc) (const void *key, Size keysize);

/*
 * Key comparison functions must have this signature.  Comparison functions
 * return zero for match, nonzero for no match.  (The comparison function
 * definition is designed to allow memcmp() and strncmp() to be used directly
 * as key comparison functions.)
 */
typedef int (*HashCompareFunc) (const void *key1, const void *key2,
								Size keysize);

/*
 * Key copying functions must have this signature.  The return value is not
 * used.  (The definition is set up to allow memcpy() and strlcpy() to be
 * used directly.)
 */
typedef void *(*HashCopyFunc) (void *dest, const void *src, Size keysize);

/*
 * Space allocation function for a hashtable.  Note: there is no free function
 * API; can't destroy a hashtable unless you use the default allocator.
 */
typedef void *(*HashAllocFunc) (Size request, void *alloc_arg);

/*
 * Hash options for hash_make() and ShmemRequestHash() macros.
 *
 * All fields are optional: zero-initialized fields use the appropriate
 * default behavior.
 */
typedef struct HASHOPTS
{
	HashValueFunc hash;			/* custom hash function (NULL for default) */
	HashCompareFunc match;		/* custom comparison function (NULL for
								 * default) */
	HashCopyFunc keycopy;		/* custom key copy function (NULL for default) */
	HashAllocFunc alloc;		/* custom allocator (NULL for default) */
	MemoryContext mcxt;			/* memory context (NULL for
								 * CurrentMemoryContext); ignored for shared
								 * memory hash tables */
	int64		num_partitions; /* partition count (0 for none) */
	bool		fixed_size;		/* if true, hash table cannot grow */
	bool		force_blobs;	/* if true, use HASH_BLOBS even for string
								 * types */
} HASHOPTS;

/*
 * Helpers to detect if a type should be hashed as a string.
 *
 * String types include: char arrays and NameData.
 * Everything else is treated as a binary blob (HASH_BLOBS).
 */
#define HASH_PTR_AS_STRING(ptr, size) \
	(pg_expr_has_type_p((ptr), char(*)[size]) || pg_expr_has_type_p((ptr), NameData *))
#define HASH_KEY_AS_STRING(entrytype, keymember) \
	HASH_PTR_AS_STRING(&((entrytype *)0)->keymember, \
					   sizeof(((entrytype *)0)->keymember))
#define HASH_TYPE_AS_STRING(type) \
	HASH_PTR_AS_STRING(pg_nullptr_of(type), sizeof(type))

/*
 * Create a hash table with minimal boilerplate.
 *
 * This is the simplest way to create a hash table. It:
 * - Derives keysize from the keymember's actual type
 * - Derives entrysize from the entrytype
 * - Automatically chooses HASH_STRINGS or HASH_BLOBS based on key type
 *   (char arrays and NameData are treated as strings)
 * - Uses CurrentMemoryContext by default
 * - Validates that keymember is at offset 0
 *
 * Optional behavior is specified via designated initializers in the
 * trailing varargs, which initialize a HASHOPTS struct.  See HASHOPTS for
 * available fields.
 *
 * NOTE: If you use char[N] to store binary data that might contain null bytes
 * and/or is not null terminated, the automatic detection will incorrectly
 * treat it as a string and use string comparison.  In such cases, pass
 * .force_blobs = true to override the automatic detection.
 *
 * Usage:
 *   typedef struct { Oid oid; char *data; } MyEntry;
 *   HTAB *h = hash_make(MyEntry, oid, "my table", 64);
 *
 *   HTAB *h = hash_make(MyEntry, oid, "my table", 64,
 *                       .mcxt = TopMemoryContext,
 *                       .num_partitions = 16);
 *
 *   HTAB *h = hash_make(MyEntry, key, "my table", 64,
 *                       .hash = my_hash_func,
 *                       .match = my_match_func);
 */
#define hash_make(entrytype, keymember, tabname, nelem, ...) \
	(StaticAssertExpr(offsetof(entrytype, keymember) == 0, \
					  #keymember " must be first member in " #entrytype), \
	 hash_make_impl((tabname), (nelem), \
					sizeof(((entrytype *)0)->keymember), \
					sizeof(entrytype), \
					HASH_KEY_AS_STRING(entrytype, keymember), \
					(HASHOPTS){__VA_ARGS__}))

/*
 * Create a hash set where the entire entry is the key.
 *
 * Like hash_make, but the key is the entire entry.  Same designated
 * initializer syntax in the varargs for optional HASHOPTS fields.
 */
#define hashset_make(entrytype, tabname, nelem, ...) \
	hash_make_impl((tabname), (nelem), sizeof(entrytype), sizeof(entrytype), \
				   HASH_TYPE_AS_STRING(entrytype), \
				   (HASHOPTS){__VA_ARGS__})

/*
 * Implementation function for hash_make/hashset_make macros.  Not meant to
 * be called directly.
 *
 * If string_key is true, the key is treated as a null-terminated string.
 */
extern HTAB *hash_make_impl(const char *tabname, int64 nelem,
							Size keysize, Size entrysize,
							bool string_key,
							HASHOPTS opts);

/*
 * HASHELEMENT is the private part of a hashtable entry.  The caller's data
 * follows the HASHELEMENT structure (on a MAXALIGN'd boundary).  The hash key
 * is expected to be at the start of the caller's hash entry data structure.
 */
typedef struct HASHELEMENT
{
	struct HASHELEMENT *link;	/* link to next entry in same bucket */
	uint32		hashvalue;		/* hash function result for this entry */
} HASHELEMENT;

/* Hash table header struct is an opaque type known only within dynahash.c */
typedef struct HASHHDR HASHHDR;

/*
 * Parameter data structure for hash_create (which is the low-level method of
 * initializing hash tables, hash_make macros are preferred)
 * Only those fields indicated by hash_flags need be set
 */
typedef struct HASHCTL
{
	/* Used if HASH_PARTITION flag is set: */
	int64		num_partitions; /* # partitions (must be power of 2) */
	/* Used if HASH_ELEM flag is set (which is now required): */
	Size		keysize;		/* hash key length in bytes */
	Size		entrysize;		/* total user element size in bytes */
	/* Used if HASH_FUNCTION flag is set: */
	HashValueFunc hash;			/* hash function */
	/* Used if HASH_COMPARE flag is set: */
	HashCompareFunc match;		/* key comparison function */
	/* Used if HASH_KEYCOPY flag is set: */
	HashCopyFunc keycopy;		/* key copying function */
	/* Used if HASH_ALLOC flag is set: */
	HashAllocFunc alloc;		/* memory allocator */
	void	   *alloc_arg;		/* opaque argument passed to allocator */
	/* Used if HASH_CONTEXT flag is set: */
	MemoryContext hcxt;			/* memory context to use for allocations */
	/* Used if HASH_ATTACH flag is set: */
	HASHHDR    *hctl;			/* location of header in shared mem */
} HASHCTL;

/* Flag bits for hash_create; most indicate which parameters are supplied */
#define HASH_PARTITION	0x0001	/* Hashtable is used w/partitioned locking */
/* 0x0002 is unused */
/* 0x0004 is unused */
#define HASH_ELEM		0x0008	/* Set keysize and entrysize (now required!) */
#define HASH_STRINGS	0x0010	/* Select support functions for string keys */
#define HASH_BLOBS		0x0020	/* Select support functions for binary keys */
#define HASH_FUNCTION	0x0040	/* Set user defined hash function */
#define HASH_COMPARE	0x0080	/* Set user defined comparison function */
#define HASH_KEYCOPY	0x0100	/* Set user defined key-copying function */
#define HASH_ALLOC		0x0200	/* Set memory allocator */
#define HASH_CONTEXT	0x0400	/* Set memory allocation context */
#define HASH_SHARED_MEM 0x0800	/* Hashtable is in shared memory */
#define HASH_ATTACH		0x1000	/* Do not initialize hctl */
#define HASH_FIXED_SIZE 0x2000	/* Initial size is a hard limit */

/* max_dsize value to indicate expansible directory */
#define NO_MAX_DSIZE			(-1)

/* hash_search operations */
typedef enum
{
	HASH_FIND,
	HASH_ENTER,
	HASH_REMOVE,
	HASH_ENTER_NULL,
} HASHACTION;

/* hash_seq status (should be considered an opaque type by callers) */
typedef struct
{
	HTAB	   *hashp;
	uint32		curBucket;		/* index of current bucket */
	HASHELEMENT *curEntry;		/* current entry in bucket */
	bool		hasHashvalue;	/* true if hashvalue was provided */
	uint32		hashvalue;		/* hashvalue to start seqscan over hash */
} HASH_SEQ_STATUS;

/*
 * prototypes for functions in dynahash.c
 */
extern HTAB *hash_create(const char *tabname, int64 nelem,
						 const HASHCTL *info, int flags);
extern void hash_opts_init(HASHCTL *ctl, int *flags,
						   Size keysize, Size entrysize, bool string_key,
						   const HASHOPTS *opts);
extern void hash_destroy(HTAB *hashp);
extern void hash_stats(const char *caller, HTAB *hashp);
extern void *hash_search(HTAB *hashp, const void *keyPtr, HASHACTION action,
						 bool *foundPtr);
extern uint32 get_hash_value(HTAB *hashp, const void *keyPtr);
extern void *hash_search_with_hash_value(HTAB *hashp, const void *keyPtr,
										 uint32 hashvalue, HASHACTION action,
										 bool *foundPtr);
extern bool hash_update_hash_key(HTAB *hashp, void *existingEntry,
								 const void *newKeyPtr);
extern int64 hash_get_num_entries(HTAB *hashp);
extern void hash_seq_init(HASH_SEQ_STATUS *status, HTAB *hashp);
extern void hash_seq_init_with_hash_value(HASH_SEQ_STATUS *status,
										  HTAB *hashp,
										  uint32 hashvalue);
extern void *hash_seq_search(HASH_SEQ_STATUS *status);
extern void hash_seq_term(HASH_SEQ_STATUS *status);
extern void hash_freeze(HTAB *hashp);
extern Size hash_estimate_size(int64 num_entries, Size entrysize);
extern void AtEOXact_HashTables(bool isCommit);
extern void AtEOSubXact_HashTables(bool isCommit, int nestDepth);

#endif							/* HSEARCH_H */
