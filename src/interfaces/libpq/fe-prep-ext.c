/*-------------------------------------------------------------------------
 *
 * fe-prep-ext.c
 *	  Extended prepared statement API with cached result metadata
 *
 * This file implements PQprepareExt/PQexecPreparedExt which maintain
 * cached result metadata on the client side, allowing efficient use
 * of the minimal_describe protocol extension.
 *
 * Portions Copyright (c) 1996-2025, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * IDENTIFICATION
 *	  src/interfaces/libpq/fe-prep-ext.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres_fe.h"

#include <string.h>

#include "libpq-fe.h"
#include "libpq-int.h"

/*
 * PQprepareExt: Prepare a statement and return a handle with cached metadata
 *
 * This creates a prepared statement on the server with an automatically
 * generated name, and returns a PGpreparedStmt handle that caches result
 * metadata for efficient reuse with minimal_describe.
 */
PGpreparedStmt *
PQprepareExt(PGconn *conn, const char *query, int nParams, const Oid *paramTypes)
{
	PGpreparedStmt *stmt;
	PGresult   *res;
	char		stmtName[64];
	static unsigned int counter = 0;

	if (!conn || !query)
		return NULL;

	/* Generate a unique statement name */
	snprintf(stmtName, sizeof(stmtName), "pqprepext_%u_%p", counter++, (void *) conn);

	/*
	 * Prepare the statement on the server. We use the async API directly
	 * instead of PQprepare because PQprepare rejects minimal_describe.
	 */
	if (!PQsendPrepare(conn, stmtName, query, nParams, paramTypes))
		return NULL;

	res = PQgetResult(conn);
	if (!res || PQresultStatus(res) != PGRES_COMMAND_OK)
	{
		/* Error during preparation - caller can check conn->errorMessage */
		if (res)
			PQclear(res);
		return NULL;
	}
	PQclear(res);

	/* Consume any remaining results */
	while ((res = PQgetResult(conn)) != NULL)
		PQclear(res);

	/* Allocate and initialize the statement handle */
	stmt = (PGpreparedStmt *) malloc(sizeof(PGpreparedStmt));
	if (!stmt)
		return NULL;

	stmt->conn = conn;
	stmt->stmtName = strdup(stmtName);
	stmt->nParams = nParams;
	stmt->paramTypes = NULL;
	stmt->nfields = 0;
	stmt->attDescs = NULL;
	stmt->hasResultDesc = false;

	if (nParams > 0 && paramTypes)
	{
		stmt->paramTypes = (Oid *) malloc(nParams * sizeof(Oid));
		if (stmt->paramTypes)
			memcpy(stmt->paramTypes, paramTypes, nParams * sizeof(Oid));
	}

	return stmt;
}

/*
 * PQexecPreparedExt: Execute a prepared statement using its handle
 *
 * This executes the prepared statement and caches any result metadata
 * received in the RowDescription message. On subsequent executions with
 * minimal_describe enabled, if no RowDescription is received, the cached
 * metadata is reused.
 *
 * Unlike PQexecPrepared, this function uses the async API internally
 * to properly handle the case where the server skips sending RowDescription.
 */
PGresult *
PQexecPreparedExt(PGpreparedStmt *stmt,
				  int nParams,
				  const char *const *paramValues,
				  const int *paramLengths,
				  const int *paramFormats,
				  int resultFormat)
{
	PGresult   *res;
	PGconn	   *conn;

	if (!stmt || !stmt->conn)
		return NULL;

	conn = stmt->conn;

	/*
	 * Use async API to execute the statement.
	 */
	if (!PQsendQueryPrepared(conn, stmt->stmtName, nParams,
							 paramValues, paramLengths, paramFormats,
							 resultFormat))
		return PQmakeEmptyPGresult(conn, PGRES_FATAL_ERROR);

	/*
	 * If we have cached metadata from a previous execution, pre-populate
	 * conn->result with it. This allows minimal_describe to work: when the
	 * server skips RowDescription, the DataRow handler will find an already
	 * initialized result object.
	 *
	 * We directly reference the cached attDescs instead of copying them. If
	 * the server DOES send a RowDescription (because types changed), the
	 * getRowDescriptions handler in fe-protocol3.c will allocate new
	 * attDescs, and we'll update our cache afterwards.
	 */
	if (stmt->hasResultDesc && stmt->attDescs)
	{
		conn->result = PQmakeEmptyPGresult(conn, PGRES_TUPLES_OK);
		if (conn->result)
		{
			conn->result->numAttributes = stmt->nfields;
			conn->result->attDescs = stmt->attDescs;
		}
	}

	/* Wait for the result */
	res = PQgetResult(conn);
	if (!res)
		return PQmakeEmptyPGresult(conn, PGRES_FATAL_ERROR);

	/*
	 * If this result has column metadata that's different from our cached
	 * metadata, update the cache. This happens when we receive a
	 * RowDescription message (because types changed).
	 *
	 * If res->attDescs == stmt->attDescs, it means the server sent no
	 * RowDescription and we're using our cached metadata, so no update
	 * needed.
	 */
	if (res->numAttributes > 0 && res->attDescs && res->attDescs != stmt->attDescs)
	{
		/* Free old cached metadata if any */
		if (stmt->attDescs)
		{
			for (int i = 0; i < stmt->nfields; i++)
			{
				if (stmt->attDescs[i].name)
					free(stmt->attDescs[i].name);
			}
			free(stmt->attDescs);
		}

		/* Cache the new metadata */
		stmt->nfields = res->numAttributes;
		stmt->attDescs = (PGresAttDesc *) malloc(stmt->nfields * sizeof(PGresAttDesc));
		if (stmt->attDescs)
		{
			memcpy(stmt->attDescs, res->attDescs, stmt->nfields * sizeof(PGresAttDesc));
			/* Duplicate the name strings so we own them */
			for (int i = 0; i < stmt->nfields; i++)
			{
				if (res->attDescs[i].name)
					stmt->attDescs[i].name = strdup(res->attDescs[i].name);
			}
			stmt->hasResultDesc = true;
		}
	}

	/*
	 * Consume any additional results (should be NULL for single-result
	 * queries)
	 */
	{
		PGresult   *res2;

		while ((res2 = PQgetResult(conn)) != NULL)
			PQclear(res2);
	}

	return res;
}

/*
 * PQclosePreparedExt: Close and deallocate a prepared statement
 *
 * Note: This function does NOT send DEALLOCATE to the server. The prepared
 * statement will be automatically cleaned up when the connection closes.
 * This design avoids use-after-free issues when the connection is closed
 * before the statement handle.
 */
void
PQclosePreparedExt(PGpreparedStmt *stmt)
{
	if (!stmt)
		return;

	/* Free client-side resources */
	if (stmt->stmtName)
		free(stmt->stmtName);
	if (stmt->paramTypes)
		free(stmt->paramTypes);
	if (stmt->attDescs)
	{
		/* Free each name string in the cached descriptors */
		for (int i = 0; i < stmt->nfields; i++)
		{
			if (stmt->attDescs[i].name)
				free(stmt->attDescs[i].name);
		}
		free(stmt->attDescs);
	}
	free(stmt);
}

/*
 * PQpreparedNfields: Get number of result columns
 */
int
PQpreparedNfields(PGpreparedStmt *stmt)
{
	if (!stmt || !stmt->hasResultDesc)
		return 0;
	return stmt->nfields;
}

/*
 * PQpreparedFname: Get column name
 */
char *
PQpreparedFname(PGpreparedStmt *stmt, int field_num)
{
	if (!stmt || !stmt->hasResultDesc ||
		field_num < 0 || field_num >= stmt->nfields)
		return NULL;
	return stmt->attDescs[field_num].name;
}

/*
 * PQpreparedFtype: Get column type OID
 */
Oid
PQpreparedFtype(PGpreparedStmt *stmt, int field_num)
{
	if (!stmt || !stmt->hasResultDesc ||
		field_num < 0 || field_num >= stmt->nfields)
		return InvalidOid;
	return stmt->attDescs[field_num].typid;
}

/*
 * PQpreparedFmod: Get column type modifier
 */
int
PQpreparedFmod(PGpreparedStmt *stmt, int field_num)
{
	if (!stmt || !stmt->hasResultDesc ||
		field_num < 0 || field_num >= stmt->nfields)
		return 0;
	return stmt->attDescs[field_num].atttypmod;
}
