/*-------------------------------------------------------------------------
 *
 * uuid.c
 *	  Functions for the built-in type "uuid".
 *
 * Copyright (c) 2007-2024, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/backend/utils/adt/uuid.c
 *
 *-------------------------------------------------------------------------
 */

#include "postgres.h"

#include <sys/time.h>

#include "access/xlog.h"
#include "common/hashfn.h"
#include "lib/hyperloglog.h"
#include "libpq/pqformat.h"
#include "port/pg_bswap.h"
#include "utils/builtins.h"
#include "utils/datetime.h"
#include "utils/guc.h"
#include "utils/sortsupport.h"
#include "utils/timestamp.h"
#include "utils/uuid.h"

/* sortsupport for uuid */
typedef struct
{
	int64		input_count;	/* number of non-null values seen */
	bool		estimating;		/* true if estimating cardinality */

	hyperLogLogState abbr_card; /* cardinality estimator */
} uuid_sortsupport_state;

static void string_to_uuid(const char *source, pg_uuid_t *uuid, Node *escontext);
static int	uuid_internal_cmp(const pg_uuid_t *arg1, const pg_uuid_t *arg2);
static int	uuid_fast_cmp(Datum x, Datum y, SortSupport ssup);
static bool uuid_abbrev_abort(int memtupcount, SortSupport ssup);
static Datum uuid_abbrev_convert(Datum original, SortSupport ssup);

Datum
uuid_in(PG_FUNCTION_ARGS)
{
	char	   *uuid_str = PG_GETARG_CSTRING(0);
	pg_uuid_t  *uuid;

	uuid = (pg_uuid_t *) palloc(sizeof(*uuid));
	string_to_uuid(uuid_str, uuid, fcinfo->context);
	PG_RETURN_UUID_P(uuid);
}

Datum
uuid_out(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *uuid = PG_GETARG_UUID_P(0);
	static const char hex_chars[] = "0123456789abcdef";
	char	   *buf,
			   *p;
	int			i;

	/* counts for the four hyphens and the zero-terminator */
	buf = palloc(2 * UUID_LEN + 5);
	p = buf;
	for (i = 0; i < UUID_LEN; i++)
	{
		int			hi;
		int			lo;

		/*
		 * We print uuid values as a string of 8, 4, 4, 4, and then 12
		 * hexadecimal characters, with each group is separated by a hyphen
		 * ("-"). Therefore, add the hyphens at the appropriate places here.
		 */
		if (i == 4 || i == 6 || i == 8 || i == 10)
			*p++ = '-';

		hi = uuid->data[i] >> 4;
		lo = uuid->data[i] & 0x0F;

		*p++ = hex_chars[hi];
		*p++ = hex_chars[lo];
	}
	*p = '\0';

	PG_RETURN_CSTRING(buf);
}

/*
 * We allow UUIDs as a series of 32 hexadecimal digits with an optional dash
 * after each group of 4 hexadecimal digits, and optionally surrounded by {}.
 * (The canonical format 8x-4x-4x-4x-12x, where "nx" means n hexadecimal
 * digits, is the only one used for output.)
 */
static void
string_to_uuid(const char *source, pg_uuid_t *uuid, Node *escontext)
{
	const char *src = source;
	bool		braces = false;
	int			i;

	if (src[0] == '{')
	{
		src++;
		braces = true;
	}

	for (i = 0; i < UUID_LEN; i++)
	{
		char		str_buf[3];

		if (src[0] == '\0' || src[1] == '\0')
			goto syntax_error;
		memcpy(str_buf, src, 2);
		if (!isxdigit((unsigned char) str_buf[0]) ||
			!isxdigit((unsigned char) str_buf[1]))
			goto syntax_error;

		str_buf[2] = '\0';
		uuid->data[i] = (unsigned char) strtoul(str_buf, NULL, 16);
		src += 2;
		if (src[0] == '-' && (i % 2) == 1 && i < UUID_LEN - 1)
			src++;
	}

	if (braces)
	{
		if (*src != '}')
			goto syntax_error;
		src++;
	}

	if (*src != '\0')
		goto syntax_error;

	return;

syntax_error:
	ereturn(escontext,,
			(errcode(ERRCODE_INVALID_TEXT_REPRESENTATION),
			 errmsg("invalid input syntax for type %s: \"%s\"",
					"uuid", source)));
}

Datum
uuid_recv(PG_FUNCTION_ARGS)
{
	StringInfo	buffer = (StringInfo) PG_GETARG_POINTER(0);
	pg_uuid_t  *uuid;

	uuid = (pg_uuid_t *) palloc(UUID_LEN);
	memcpy(uuid->data, pq_getmsgbytes(buffer, UUID_LEN), UUID_LEN);
	PG_RETURN_POINTER(uuid);
}

Datum
uuid_send(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *uuid = PG_GETARG_UUID_P(0);
	StringInfoData buffer;

	pq_begintypsend(&buffer);
	pq_sendbytes(&buffer, uuid->data, UUID_LEN);
	PG_RETURN_BYTEA_P(pq_endtypsend(&buffer));
}

/* internal uuid compare function */
static int
uuid_internal_cmp(const pg_uuid_t *arg1, const pg_uuid_t *arg2)
{
	return memcmp(arg1->data, arg2->data, UUID_LEN);
}

Datum
uuid_lt(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *arg1 = PG_GETARG_UUID_P(0);
	pg_uuid_t  *arg2 = PG_GETARG_UUID_P(1);

	PG_RETURN_BOOL(uuid_internal_cmp(arg1, arg2) < 0);
}

Datum
uuid_le(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *arg1 = PG_GETARG_UUID_P(0);
	pg_uuid_t  *arg2 = PG_GETARG_UUID_P(1);

	PG_RETURN_BOOL(uuid_internal_cmp(arg1, arg2) <= 0);
}

Datum
uuid_eq(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *arg1 = PG_GETARG_UUID_P(0);
	pg_uuid_t  *arg2 = PG_GETARG_UUID_P(1);

	PG_RETURN_BOOL(uuid_internal_cmp(arg1, arg2) == 0);
}

Datum
uuid_ge(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *arg1 = PG_GETARG_UUID_P(0);
	pg_uuid_t  *arg2 = PG_GETARG_UUID_P(1);

	PG_RETURN_BOOL(uuid_internal_cmp(arg1, arg2) >= 0);
}

Datum
uuid_gt(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *arg1 = PG_GETARG_UUID_P(0);
	pg_uuid_t  *arg2 = PG_GETARG_UUID_P(1);

	PG_RETURN_BOOL(uuid_internal_cmp(arg1, arg2) > 0);
}

Datum
uuid_ne(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *arg1 = PG_GETARG_UUID_P(0);
	pg_uuid_t  *arg2 = PG_GETARG_UUID_P(1);

	PG_RETURN_BOOL(uuid_internal_cmp(arg1, arg2) != 0);
}

/* handler for btree index operator */
Datum
uuid_cmp(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *arg1 = PG_GETARG_UUID_P(0);
	pg_uuid_t  *arg2 = PG_GETARG_UUID_P(1);

	PG_RETURN_INT32(uuid_internal_cmp(arg1, arg2));
}

/*
 * Sort support strategy routine
 */
Datum
uuid_sortsupport(PG_FUNCTION_ARGS)
{
	SortSupport ssup = (SortSupport) PG_GETARG_POINTER(0);

	ssup->comparator = uuid_fast_cmp;
	ssup->ssup_extra = NULL;

	if (ssup->abbreviate)
	{
		uuid_sortsupport_state *uss;
		MemoryContext oldcontext;

		oldcontext = MemoryContextSwitchTo(ssup->ssup_cxt);

		uss = palloc(sizeof(uuid_sortsupport_state));
		uss->input_count = 0;
		uss->estimating = true;
		initHyperLogLog(&uss->abbr_card, 10);

		ssup->ssup_extra = uss;

		ssup->comparator = ssup_datum_unsigned_cmp;
		ssup->abbrev_converter = uuid_abbrev_convert;
		ssup->abbrev_abort = uuid_abbrev_abort;
		ssup->abbrev_full_comparator = uuid_fast_cmp;

		MemoryContextSwitchTo(oldcontext);
	}

	PG_RETURN_VOID();
}

/*
 * SortSupport comparison func
 */
static int
uuid_fast_cmp(Datum x, Datum y, SortSupport ssup)
{
	pg_uuid_t  *arg1 = DatumGetUUIDP(x);
	pg_uuid_t  *arg2 = DatumGetUUIDP(y);

	return uuid_internal_cmp(arg1, arg2);
}

/*
 * Callback for estimating effectiveness of abbreviated key optimization.
 *
 * We pay no attention to the cardinality of the non-abbreviated data, because
 * there is no equality fast-path within authoritative uuid comparator.
 */
static bool
uuid_abbrev_abort(int memtupcount, SortSupport ssup)
{
	uuid_sortsupport_state *uss = ssup->ssup_extra;
	double		abbr_card;

	if (memtupcount < 10000 || uss->input_count < 10000 || !uss->estimating)
		return false;

	abbr_card = estimateHyperLogLog(&uss->abbr_card);

	/*
	 * If we have >100k distinct values, then even if we were sorting many
	 * billion rows we'd likely still break even, and the penalty of undoing
	 * that many rows of abbrevs would probably not be worth it.  Stop even
	 * counting at that point.
	 */
	if (abbr_card > 100000.0)
	{
#ifdef TRACE_SORT
		if (trace_sort)
			elog(LOG,
				 "uuid_abbrev: estimation ends at cardinality %f"
				 " after " INT64_FORMAT " values (%d rows)",
				 abbr_card, uss->input_count, memtupcount);
#endif
		uss->estimating = false;
		return false;
	}

	/*
	 * Target minimum cardinality is 1 per ~2k of non-null inputs.  0.5 row
	 * fudge factor allows us to abort earlier on genuinely pathological data
	 * where we've had exactly one abbreviated value in the first 2k
	 * (non-null) rows.
	 */
	if (abbr_card < uss->input_count / 2000.0 + 0.5)
	{
#ifdef TRACE_SORT
		if (trace_sort)
			elog(LOG,
				 "uuid_abbrev: aborting abbreviation at cardinality %f"
				 " below threshold %f after " INT64_FORMAT " values (%d rows)",
				 abbr_card, uss->input_count / 2000.0 + 0.5, uss->input_count,
				 memtupcount);
#endif
		return true;
	}

#ifdef TRACE_SORT
	if (trace_sort)
		elog(LOG,
			 "uuid_abbrev: cardinality %f after " INT64_FORMAT
			 " values (%d rows)", abbr_card, uss->input_count, memtupcount);
#endif

	return false;
}

/*
 * Conversion routine for sortsupport.  Converts original uuid representation
 * to abbreviated key representation.  Our encoding strategy is simple -- pack
 * the first `sizeof(Datum)` bytes of uuid data into a Datum (on little-endian
 * machines, the bytes are stored in reverse order), and treat it as an
 * unsigned integer.
 */
static Datum
uuid_abbrev_convert(Datum original, SortSupport ssup)
{
	uuid_sortsupport_state *uss = ssup->ssup_extra;
	pg_uuid_t  *authoritative = DatumGetUUIDP(original);
	Datum		res;

	memcpy(&res, authoritative->data, sizeof(Datum));
	uss->input_count += 1;

	if (uss->estimating)
	{
		uint32		tmp;

#if SIZEOF_DATUM == 8
		tmp = (uint32) res ^ (uint32) ((uint64) res >> 32);
#else							/* SIZEOF_DATUM != 8 */
		tmp = (uint32) res;
#endif

		addHyperLogLog(&uss->abbr_card, DatumGetUInt32(hash_uint32(tmp)));
	}

	/*
	 * Byteswap on little-endian machines.
	 *
	 * This is needed so that ssup_datum_unsigned_cmp() (an unsigned integer
	 * 3-way comparator) works correctly on all platforms.  If we didn't do
	 * this, the comparator would have to call memcmp() with a pair of
	 * pointers to the first byte of each abbreviated key, which is slower.
	 */
	res = DatumBigEndianToNative(res);

	return res;
}

/* hash index support */
Datum
uuid_hash(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *key = PG_GETARG_UUID_P(0);

	return hash_any(key->data, UUID_LEN);
}

Datum
uuid_hash_extended(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *key = PG_GETARG_UUID_P(0);

	return hash_any_extended(key->data, UUID_LEN, PG_GETARG_INT64(1));
}

/*
 * Routine to generate UUID version 4.
 * All UUID bytes are filled with strong random numbers except version and
 * variant 0b10 bits.
 */
Datum
gen_random_uuid(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *uuid = palloc(UUID_LEN);

	if (!pg_strong_random(uuid, UUID_LEN))
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("could not generate random values")));

	/*
	 * Set magic numbers for a "version 4" (pseudorandom) UUID, see
	 * http://tools.ietf.org/html/rfc4122#section-4.4
	 */
	uuid->data[6] = (uuid->data[6] & 0x0f) | 0x40;	/* time_hi_and_version */
	uuid->data[8] = (uuid->data[8] & 0x3f) | 0x80;	/* clock_seq_hi_and_reserved */

	PG_RETURN_UUID_P(uuid);
}

static uint32_t sequence_counter;
static uint64_t previous_timestamp = 0;

/*
 * Routine to generate UUID version 7.
 * Following description is taken from RFC draft and slightly extended to
 * reflect implementation specific choices.
 *
 * UUIDv7 Field and Bit Layout:
 * ----------
 *  0                   1                   2                   3
 *  0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
 * +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
 * |                           unix_ts_ms                          |
 * +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
 * |          unix_ts_ms           |  ver  |       rand_a          |
 * +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
 * |var|                        rand_b                             |
 * +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
 * |                            rand_b                             |
 * +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
 *
 * unix_ts_ms:
 *  48 bit big-endian unsigned number of Unix epoch timestamp in milliseconds
 *  as per Section 6.1. Occupies bits 0 through 47 (octets 0-5).
 *
 * ver:
 *  The 4 bit version field as defined by Section 4.2, set to 0b0111 (7).
 *  Occupies bits 48 through 51 of octet 6.
 *
 * rand_a:
 *  Most significant 12 bits of 18-bit counter. This counter is designed to
 *  guarantee additional monotonicity as per Section 6.2 (Method 2). rand_a
 *  occupies bits 52 through 63 (octets 6-7).
 *
 * var:
 *  The 2 bit variant field as defined by Section 4.1, set to 0b10. Occupies
 *  bits 64 and 65 of octet 8.
 *
 * rand_b:
 *  Starting 6 bits are least significant 6 bits of a counter. The final 56
 *  bits filled with pseudo-random data to provide uniqueness as per
 *  Section 6.9. rand_b Occupies bits 66 through 127 (octets 8-15).
 * ----------
 *
 * Fixed-Length Dedicated Counter Bits (Method 1) MAY use the left-most bits of
 * rand_b as additional counter bits. We choose size 18 to reuse all space of
 * bytes that are touched by ver and var fields + rand_a bytes between them.
 * Whenever timestamp unix_ts_ms is moving forward, this counter bits are
 * reinitialized. Reinilialization always sets most significant bit to 0, other
 * bits are initialized with random numbers. This gives as approximately 262K
 * UUIDs within one millisecond without overflow. This ougth to be enough for
 * most practical purposes. Whenever counter overflow happens, this overflow is
 * translated to increment of unix_ts_ms. So generation of UUIDs at a rate
 * higher than 262MHz in the same backend might lead to using timestamps ahead
 * of time.
 *
 * We're not using the "Replace Left-Most Random Bits with Increased Clock
 * Precision" method Section 6.2 (Method 3), because of portability concerns.
 * It's unclear if all supported platforms can provide reliable microsocond
 * precision time.
 *
 * All UUID generator state is backend-local. For UUIDs generated in one
 * backend we guarantee monotonicity. UUIDs generated on different backends
 * will be mostly monotonic if they are generated at frequences less than 1KHz,
 * but this monotonicity is not strictly guaranteed. UUIDs generated on
 * different nodes are mostly monotonic with regards to possible clock drift.
 * Uniqueness of UUIDs generated at the same timestamp across different
 * backends and/or nodes is guaranteed by using random bits. Since we're still
 * using 56 bits of random data in rand_b, so we're not expecting any
 * collisions within the same millisecond.
 */
Datum
uuidv7(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *uuid = palloc(UUID_LEN);
	uint64_t	tms;
	struct timeval tp;
	bool		increment_counter;

	gettimeofday(&tp, NULL);
	tms = ((uint64_t) tp.tv_sec) * 1000 + (tp.tv_usec) / 1000;
	/* time from clock is protected from backward leaps */
	increment_counter = (tms <= previous_timestamp);

	if (increment_counter)
	{
		/*
		 * Time did not advance from the previous generation, we must
		 * increment counter
		 */
		++sequence_counter;
		if (sequence_counter > 0x3ffff)
		{
			/* We only have 18-bit counter */
			sequence_counter = 0;
			previous_timestamp++;
		}

		/* protection from leap backward */
		tms = previous_timestamp;

	}
	else
	{
		/* read randomly initialized bits of counter */
		sequence_counter = 0;

		previous_timestamp = tms;
	}

	/* Fill in time part */
	uuid->data[0] = (unsigned char) (tms >> 40);
	uuid->data[1] = (unsigned char) (tms >> 32);
	uuid->data[2] = (unsigned char) (tms >> 24);
	uuid->data[3] = (unsigned char) (tms >> 16);
	uuid->data[4] = (unsigned char) (tms >> 8);
	uuid->data[5] = (unsigned char) tms;
	/* most significant 4 bits of 18-bit counter */
	uuid->data[6] = (unsigned char) (sequence_counter >> 14);
	/* next 8 bits */
	uuid->data[7] = (unsigned char) (sequence_counter >> 6);
	/* least significant 6 bits */
	uuid->data[8] = (unsigned char) (sequence_counter);

	/* fill everything after the timestamp and counter with random bytes */
	if (!pg_strong_random(&uuid->data[8], UUID_LEN - 8))
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("could not generate random values")));

	/*
	 * Set magic numbers for a "version 7" (pseudorandom) UUID, see
	 * https://datatracker.ietf.org/doc/html/draft-ietf-uuidrev-rfc4122bis
	 */
	/* set version field, top four bits are 0, 1, 1, 1 */
	uuid->data[6] = (uuid->data[6] & 0x0f) | 0x70;
	/* set variant field, top two bits are 1, 0 */
	uuid->data[8] = (uuid->data[8] & 0x3f) | 0x80;

	PG_RETURN_UUID_P(uuid);
}

/*
 * Routine to extract UUID version from variant 0b10
 * Returns NULL if UUID is not 0b10 or version is not 1,6 or7.
 */
Datum
uuid_extract_timestamp(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *uuid = PG_GETARG_UUID_P(0);
	TimestampTz ts;
	uint64_t	tms;

	if ((uuid->data[8] & 0xc0) != 0x80)
		PG_RETURN_NULL();

	if ((uuid->data[6] & 0xf0) == 0x70)
	{
		tms = uuid->data[5];
		tms += ((uint64_t) uuid->data[4]) << 8;
		tms += ((uint64_t) uuid->data[3]) << 16;
		tms += ((uint64_t) uuid->data[2]) << 24;
		tms += ((uint64_t) uuid->data[1]) << 32;
		tms += ((uint64_t) uuid->data[0]) << 40;

		/* convert ms to us, then adjust */
		ts = (TimestampTz) (tms * 1000) -
			(POSTGRES_EPOCH_JDATE - UNIX_EPOCH_JDATE) * SECS_PER_DAY * USECS_PER_SEC;

		PG_RETURN_TIMESTAMPTZ(ts);
	}

	if ((uuid->data[6] & 0xf0) == 0x10)
	{
		tms = ((uint64_t) uuid->data[0]) << 24;
		tms += ((uint64_t) uuid->data[1]) << 16;
		tms += ((uint64_t) uuid->data[2]) << 8;
		tms += ((uint64_t) uuid->data[3]);
		tms += ((uint64_t) uuid->data[4]) << 40;
		tms += ((uint64_t) uuid->data[5]) << 32;
		tms += (((uint64_t) uuid->data[6]) & 0xf) << 56;
		tms += ((uint64_t) uuid->data[7]) << 48;

		/* convert 100-ns intervals to us, then adjust */
		ts = (TimestampTz) (tms / 10) -
			((uint64_t) POSTGRES_EPOCH_JDATE - GREGORIAN_EPOCH_JDATE) * SECS_PER_DAY * USECS_PER_SEC;

		PG_RETURN_TIMESTAMPTZ(ts);
	}

	if ((uuid->data[6] & 0xf0) == 0x60)
	{
		tms = ((uint64_t) uuid->data[0]) << 52;
		tms += ((uint64_t) uuid->data[1]) << 44;
		tms += ((uint64_t) uuid->data[2]) << 36;
		tms += ((uint64_t) uuid->data[3]) << 28;
		tms += ((uint64_t) uuid->data[4]) << 20;
		tms += ((uint64_t) uuid->data[5]) << 12;
		tms += (((uint64_t) uuid->data[6]) & 0xf) << 8;
		tms += ((uint64_t) uuid->data[7]);

		/* convert 100-ns intervals to us, then adjust */
		ts = (TimestampTz) (tms / 10) -
			((uint64_t) POSTGRES_EPOCH_JDATE - GREGORIAN_EPOCH_JDATE) * SECS_PER_DAY * USECS_PER_SEC;

		PG_RETURN_TIMESTAMPTZ(ts);
	}

	PG_RETURN_NULL();
}

/*
 * Routine to extract UUID version from variant 0b10
 * Returns NULL if UUID is not 0b10
 */
Datum
uuid_extract_version(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *uuid = PG_GETARG_UUID_P(0);
	uint16_t	result;

	if ((uuid->data[8] & 0xc0) != 0x80)
		PG_RETURN_NULL();
	result = uuid->data[6] >> 4;

	PG_RETURN_UINT16(result);
}

/*
 * Routine to extract UUID variant. Can return only 0, 0b10, 0b110 and 0b111.
 */
Datum
uuid_extract_variant(PG_FUNCTION_ARGS)
{
	pg_uuid_t  *uuid = PG_GETARG_UUID_P(0);
	uint16_t	result;

	/*-----------
	 * The contents of the variant field, where the letter "x" indicates a
	 * "don't-care" value.
	 * Msb0		Msb1	Msb2	Msb3	Variant	Description
	 * 0		x		x		x		1-7		Reserved, NCS backward
	 * 											compatibility and includes Nil
	 * 											UUID as per Section 5.9.
	 * 1		0		x		x		8-9,A-B	The variant specified in RFC.
	 * 1		1		0		x		C-D		Reserved, Microsoft Corporation
	 * 											backward compatibility.
	 * 1		1		1		x		E-F		Reserved for future definition
	 * 											and includes Max UUID as per
	 * 											Section 5.10 of RFC.
	 *-----------
	 */

	uint8_t		nibble = uuid->data[8] >> 4;

	if (nibble < 8)
		result = 0;
	else if (nibble < 0xC)
		result = 0b10;
	else if (nibble < 0xE)
		result = 0b110;
	else
		result = 0b111;

	PG_RETURN_UINT16(result);
}
