# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_saslprep/t/001_saslprep_ranges.pl.

Tests all valid UTF-8 codepoint ranges under SASLprep. This is expensive, so it
is gated behind PG_TEST_EXTRA=saslprep.
"""

from pypg import require_test_extras

pytestmark = require_test_extras("saslprep")


def test_saslprep_ranges(create_pg):
    node = create_pg("main")
    node.sql("CREATE EXTENSION test_saslprep;")
    # Among all valid UTF-8 codepoint ranges, SASLprep should never return an
    # empty password when the operation is considered a success.
    result = node.sql(
        "SELECT * FROM test_saslprep_ranges()"
        " WHERE status = 'SUCCESS' AND res IN (NULL, '')"
    )
    assert not result, f"valid codepoints returned an empty password: {result}"
