/*--------------------------------------------------------------------------
 *
 * test_cplusplusext.cpp
 *		Test that PostgreSQL headers compile with a C++ compiler.
 *
 * This file is compiled with a C++ compiler to verify that PostgreSQL
 * headers remain compatible with C++ extensions.
 *
 * Copyright (c) 2025-2026, PostgreSQL Global Development Group
 *
 * IDENTIFICATION
 *		src/test/modules/test_cplusplusext/test_cplusplusext.cpp
 *
 * -------------------------------------------------------------------------
 */

extern "C" {
#include "postgres.h"
#include "fmgr.h"
#include "nodes/pg_list.h"
#include "nodes/parsenodes.h"

PG_MODULE_MAGIC;

PG_FUNCTION_INFO_V1(test_cplusplus_compat);
}

StaticAssertDecl(sizeof(int32) == 4, "int32 should be 4 bytes");

extern "C" Datum
test_cplusplus_compat(PG_FUNCTION_ARGS)
{
	List	   *node_list = list_make1(makeNode(RangeTblRef));
	RangeTblRef *copy = copyObject(linitial_node(RangeTblRef, node_list));

	foreach_ptr(RangeTblRef, rtr, node_list) {
		rtr->rtindex++;
	}

	foreach_node(RangeTblRef, rtr, node_list) {
		rtr->rtindex++;
	}

	StaticAssertStmt(sizeof(int32) == 4, "int32 should be 4 bytes");
	(void) StaticAssertExpr(sizeof(int64) == 8, "int64 should be 8 bytes");

	pfree(copy);
	list_free_deep(node_list);

	PG_RETURN_VOID();
}
