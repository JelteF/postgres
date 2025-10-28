/*-------------------------------------------------------------------------
 *
 * instrument.c
 *	 functions for instrumentation of plan execution
 *
 *
 * Copyright (c) 2001-2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *	  src/backend/executor/instrument.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <sys/resource.h>
#include <unistd.h>

#include "executor/instrument.h"
#include "storage/aio_subsys.h"

BufferUsage pgBufferUsage;
static BufferUsage save_pgBufferUsage;

StorageIOUsage pgStorageIOUsageParallel;	/* only count parallel workers'
											 * usage */
WalUsage	pgWalUsage;
static WalUsage save_pgWalUsage;

static void BufferUsageAdd(BufferUsage *dst, const BufferUsage *add);
void		StorageIOUsageAdd(StorageIOUsage *dst, const StorageIOUsage *add);
static void WalUsageAdd(WalUsage *dst, WalUsage *add);


/* Allocate new instrumentation structure(s) */
Instrumentation *
InstrAlloc(int n, int instrument_options, bool async_mode)
{
	Instrumentation *instr;

	/* initialize all fields to zeroes, then modify as needed */
	instr = palloc0(n * sizeof(Instrumentation));
	if (instrument_options & (INSTRUMENT_BUFFERS | INSTRUMENT_TIMER | INSTRUMENT_WAL))
	{
		bool		need_buffers = (instrument_options & INSTRUMENT_BUFFERS) != 0;
		bool		need_wal = (instrument_options & INSTRUMENT_WAL) != 0;
		bool		need_timer = (instrument_options & INSTRUMENT_TIMER) != 0;
		int			i;

		for (i = 0; i < n; i++)
		{
			instr[i].need_bufusage = need_buffers;
			instr[i].need_walusage = need_wal;
			instr[i].need_timer = need_timer;
			instr[i].async_mode = async_mode;
		}
	}

	return instr;
}

/* Initialize a pre-allocated instrumentation structure. */
void
InstrInit(Instrumentation *instr, int instrument_options)
{
	memset(instr, 0, sizeof(Instrumentation));
	instr->need_bufusage = (instrument_options & INSTRUMENT_BUFFERS) != 0;
	instr->need_walusage = (instrument_options & INSTRUMENT_WAL) != 0;
	instr->need_timer = (instrument_options & INSTRUMENT_TIMER) != 0;
}

/* Entry to a plan node */
void
InstrStartNode(Instrumentation *instr)
{
	if (instr->need_timer &&
		!INSTR_TIME_SET_CURRENT_LAZY(instr->starttime))
		elog(ERROR, "InstrStartNode called twice in a row");

	/* save buffer usage totals at node entry, if needed */
	if (instr->need_bufusage)
		instr->bufusage_start = pgBufferUsage;

	if (instr->need_walusage)
		instr->walusage_start = pgWalUsage;
}

/* Exit from a plan node */
void
InstrStopNode(Instrumentation *instr, double nTuples)
{
	double		save_tuplecount = instr->tuplecount;
	instr_time	endtime;

	/* count the returned tuples */
	instr->tuplecount += nTuples;

	/* let's update the time only if the timer was requested */
	if (instr->need_timer)
	{
		if (INSTR_TIME_IS_ZERO(instr->starttime))
			elog(ERROR, "InstrStopNode called without start");

		INSTR_TIME_SET_CURRENT(endtime);
		INSTR_TIME_ACCUM_DIFF(instr->counter, endtime, instr->starttime);

		INSTR_TIME_SET_ZERO(instr->starttime);
	}

	/* Add delta of buffer usage since entry to node's totals */
	if (instr->need_bufusage)
		BufferUsageAccumDiff(&instr->bufusage,
							 &pgBufferUsage, &instr->bufusage_start);

	if (instr->need_walusage)
		WalUsageAccumDiff(&instr->walusage,
						  &pgWalUsage, &instr->walusage_start);

	/* Is this the first tuple of this cycle? */
	if (!instr->running)
	{
		instr->running = true;
		instr->firsttuple = instr->counter;
	}
	else
	{
		/*
		 * In async mode, if the plan node hadn't emitted any tuples before,
		 * this might be the first tuple
		 */
		if (instr->async_mode && save_tuplecount < 1.0)
			instr->firsttuple = instr->counter;
	}
}

/* Update tuple count */
void
InstrUpdateTupleCount(Instrumentation *instr, double nTuples)
{
	/* count the returned tuples */
	instr->tuplecount += nTuples;
}

/* Finish a run cycle for a plan node */
void
InstrEndLoop(Instrumentation *instr)
{
	/* Skip if nothing has happened, or already shut down */
	if (!instr->running)
		return;

	if (!INSTR_TIME_IS_ZERO(instr->starttime))
		elog(ERROR, "InstrEndLoop called on running node");

	/* Accumulate per-cycle statistics into totals */
	INSTR_TIME_ADD(instr->startup, instr->firsttuple);
	INSTR_TIME_ADD(instr->total, instr->counter);
	instr->ntuples += instr->tuplecount;
	instr->nloops += 1;

	/* Reset for next cycle (if any) */
	instr->running = false;
	INSTR_TIME_SET_ZERO(instr->starttime);
	INSTR_TIME_SET_ZERO(instr->counter);
	INSTR_TIME_SET_ZERO(instr->firsttuple);
	instr->tuplecount = 0;
}

/* aggregate instrumentation information */
void
InstrAggNode(Instrumentation *dst, Instrumentation *add)
{
	if (!dst->running && add->running)
	{
		dst->running = true;
		dst->firsttuple = add->firsttuple;
	}
	else if (dst->running && add->running &&
			 INSTR_TIME_LT(dst->firsttuple, add->firsttuple))
		dst->firsttuple = add->firsttuple;

	INSTR_TIME_ADD(dst->counter, add->counter);

	dst->tuplecount += add->tuplecount;
	INSTR_TIME_ADD(dst->startup, add->startup);
	INSTR_TIME_ADD(dst->total, add->total);
	dst->ntuples += add->ntuples;
	dst->ntuples2 += add->ntuples2;
	dst->nloops += add->nloops;
	dst->nfiltered1 += add->nfiltered1;
	dst->nfiltered2 += add->nfiltered2;

	/* Add delta of buffer usage since entry to node's totals */
	if (dst->need_bufusage)
		BufferUsageAdd(&dst->bufusage, &add->bufusage);

	if (dst->need_walusage)
		WalUsageAdd(&dst->walusage, &add->walusage);
}

/* note current values during parallel executor startup */
void
InstrStartParallelQuery(StorageIOUsage *storageiousage)
{
	save_pgBufferUsage = pgBufferUsage;
	save_pgWalUsage = pgWalUsage;

	if (storageiousage != NULL)
		GetStorageIOUsage(storageiousage);
}

/* report usage after parallel executor shutdown */
void
InstrEndParallelQuery(BufferUsage *bufusage, StorageIOUsage *storageiousage, WalUsage *walusage, StorageIOUsage *storageiousage_start)
{
	memset(bufusage, 0, sizeof(BufferUsage));
	BufferUsageAccumDiff(bufusage, &pgBufferUsage, &save_pgBufferUsage);

	if (storageiousage != NULL && storageiousage_start != NULL)
	{
		struct StorageIOUsage storageiousage_end;

		GetStorageIOUsage(&storageiousage_end);

		memset(storageiousage, 0, sizeof(StorageIOUsage));
		StorageIOUsageAccumDiff(storageiousage, &storageiousage_end, storageiousage_start);

		ereport(DEBUG1,
				(errmsg("Parallel worker's storage I/O times: inblock:%ld outblock:%ld",
						storageiousage->inblock, storageiousage->outblock)));
	}
	memset(walusage, 0, sizeof(WalUsage));
	WalUsageAccumDiff(walusage, &pgWalUsage, &save_pgWalUsage);
}

/* accumulate work done by workers in leader's stats */
void
InstrAccumParallelQuery(BufferUsage *bufusage, StorageIOUsage *storageiousage, WalUsage *walusage)
{
	BufferUsageAdd(&pgBufferUsage, bufusage);

	if (storageiousage != NULL)
		StorageIOUsageAdd(&pgStorageIOUsageParallel, storageiousage);
	WalUsageAdd(&pgWalUsage, walusage);
}

/* dst += add */
static void
BufferUsageAdd(BufferUsage *dst, const BufferUsage *add)
{
	dst->shared_blks_hit += add->shared_blks_hit;
	dst->shared_blks_read += add->shared_blks_read;
	dst->shared_blks_dirtied += add->shared_blks_dirtied;
	dst->shared_blks_written += add->shared_blks_written;
	dst->local_blks_hit += add->local_blks_hit;
	dst->local_blks_read += add->local_blks_read;
	dst->local_blks_dirtied += add->local_blks_dirtied;
	dst->local_blks_written += add->local_blks_written;
	dst->temp_blks_read += add->temp_blks_read;
	dst->temp_blks_written += add->temp_blks_written;
	INSTR_TIME_ADD(dst->shared_blk_read_time, add->shared_blk_read_time);
	INSTR_TIME_ADD(dst->shared_blk_write_time, add->shared_blk_write_time);
	INSTR_TIME_ADD(dst->local_blk_read_time, add->local_blk_read_time);
	INSTR_TIME_ADD(dst->local_blk_write_time, add->local_blk_write_time);
	INSTR_TIME_ADD(dst->temp_blk_read_time, add->temp_blk_read_time);
	INSTR_TIME_ADD(dst->temp_blk_write_time, add->temp_blk_write_time);
}

/* dst += add - sub */
void
BufferUsageAccumDiff(BufferUsage *dst,
					 const BufferUsage *add,
					 const BufferUsage *sub)
{
	dst->shared_blks_hit += add->shared_blks_hit - sub->shared_blks_hit;
	dst->shared_blks_read += add->shared_blks_read - sub->shared_blks_read;
	dst->shared_blks_dirtied += add->shared_blks_dirtied - sub->shared_blks_dirtied;
	dst->shared_blks_written += add->shared_blks_written - sub->shared_blks_written;
	dst->local_blks_hit += add->local_blks_hit - sub->local_blks_hit;
	dst->local_blks_read += add->local_blks_read - sub->local_blks_read;
	dst->local_blks_dirtied += add->local_blks_dirtied - sub->local_blks_dirtied;
	dst->local_blks_written += add->local_blks_written - sub->local_blks_written;
	dst->temp_blks_read += add->temp_blks_read - sub->temp_blks_read;
	dst->temp_blks_written += add->temp_blks_written - sub->temp_blks_written;
	INSTR_TIME_ACCUM_DIFF(dst->shared_blk_read_time,
						  add->shared_blk_read_time, sub->shared_blk_read_time);
	INSTR_TIME_ACCUM_DIFF(dst->shared_blk_write_time,
						  add->shared_blk_write_time, sub->shared_blk_write_time);
	INSTR_TIME_ACCUM_DIFF(dst->local_blk_read_time,
						  add->local_blk_read_time, sub->local_blk_read_time);
	INSTR_TIME_ACCUM_DIFF(dst->local_blk_write_time,
						  add->local_blk_write_time, sub->local_blk_write_time);
	INSTR_TIME_ACCUM_DIFF(dst->temp_blk_read_time,
						  add->temp_blk_read_time, sub->temp_blk_read_time);
	INSTR_TIME_ACCUM_DIFF(dst->temp_blk_write_time,
						  add->temp_blk_write_time, sub->temp_blk_write_time);
}

/* helper functions for StorageIOUsage usage accumulation */
void
StorageIOUsageAdd(StorageIOUsage *dst, const StorageIOUsage *add)
{
	dst->inblock += add->inblock;
	dst->outblock += add->outblock;
}

/* dst += add - sub */
void
StorageIOUsageAccumDiff(StorageIOUsage *dst, const StorageIOUsage *add, const StorageIOUsage *sub)
{
	dst->inblock += add->inblock - sub->inblock;
	dst->outblock += add->outblock - sub->outblock;
}

/* dst -= sub */
void
StorageIOUsageDiff(StorageIOUsage *dst, const StorageIOUsage *sub)
{
	dst->inblock -= sub->inblock;
	dst->outblock -= sub->outblock;
}

/* Captures the current storage I/O usage statistics */
void
GetStorageIOUsage(StorageIOUsage *usage)
{
	struct rusage rusage;

	/*
	 * Since getting the I/O excluding AIO workers underestimates the total
	 * I/O, don't get the I/O usage statistics when AIO worker is enabled.
	 */
	if (pgaio_workers_enabled())
		return;

	if (getrusage(RUSAGE_SELF, &rusage))
	{
		ereport(ERROR,
				(errcode(ERRCODE_SYSTEM_ERROR),
				 errmsg("getrusage() failed: %m")));
	}
	usage->inblock = rusage.ru_inblock;
	usage->outblock = rusage.ru_oublock;
}

/* helper functions for WAL usage accumulation */
static void
WalUsageAdd(WalUsage *dst, WalUsage *add)
{
	dst->wal_bytes += add->wal_bytes;
	dst->wal_records += add->wal_records;
	dst->wal_fpi += add->wal_fpi;
	dst->wal_fpi_bytes += add->wal_fpi_bytes;
	dst->wal_buffers_full += add->wal_buffers_full;
}

void
WalUsageAccumDiff(WalUsage *dst, const WalUsage *add, const WalUsage *sub)
{
	dst->wal_bytes += add->wal_bytes - sub->wal_bytes;
	dst->wal_records += add->wal_records - sub->wal_records;
	dst->wal_fpi += add->wal_fpi - sub->wal_fpi;
	dst->wal_fpi_bytes += add->wal_fpi_bytes - sub->wal_fpi_bytes;
	dst->wal_buffers_full += add->wal_buffers_full - sub->wal_buffers_full;
}
