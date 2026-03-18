/*
 * test_ext.c
 *
 * Dummy C extension for testing extension_control_path with pg_upgrade
 *
 * Portions Copyright (c) 2026, PostgreSQL Global Development Group
 */
#include "postgres.h"

#include "fmgr.h"

PG_MODULE_MAGIC;

PG_FUNCTION_INFO_V1(test_ext);

/* Confirm that C implementation of pg_expr_has_type_p works as expected on all compilers. */
StaticAssertDecl(pg_expr_has_type_p((int32) 123, int32), "int32 expression should be int32");
StaticAssertDecl(!pg_expr_has_type_p((int32) 123, int64), "int32 expression should not be int64");
StaticAssertDecl(pg_expr_has_type_p(((char (*)[10]) NULL)[0], char *),
				 "array should decay into pointer");
StaticAssertDecl(pg_expr_has_type_p((char (*)[10]) NULL, char (*)[10]),
				 "pointer to an aray should work if it has the same size");
StaticAssertDecl(!pg_expr_has_type_p((char (*)[5]) NULL, char (*)[10]),
				 "pointer to an aray should not match if it does not have the same size");
StaticAssertDecl(pg_expr_has_type_p((const int *) NULL, const int *),
				 "const pointers of same type should match");
StaticAssertDecl(!pg_expr_has_type_p((const int *) NULL, int *),
				 "const pointer should not match non-const pointer");
StaticAssertDecl(pg_expr_has_type_p((const int) 0, int),
				 "top-level const should be stripped");

Datum
test_ext(PG_FUNCTION_ARGS)
{
	ereport(NOTICE,
			(errmsg("running successful")));
	PG_RETURN_VOID();
}
