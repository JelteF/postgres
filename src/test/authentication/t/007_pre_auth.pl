
# Copyright (c) 2021-2026, PostgreSQL Global Development Group

# Tests for connection behavior prior to authentication.

use strict;
use warnings FATAL => 'all';

use PostgreSQL::Test::Cluster;
use PostgreSQL::Test::Utils;
use Test::More;

if ($ENV{enable_injection_points} ne 'yes')
{
	plan skip_all => 'Injection points not supported by this build';
}

my $node = PostgreSQL::Test::Cluster->new('primary');
$node->init;
$node->append_conf(
	'postgresql.conf', q[
log_connections = 'receipt,authentication'
]);

$node->start;

# Check if the extension injection_points is available, as it may be
# possible that this script is run with installcheck, where the module
# would not be installed by default.
if (!$node->check_extension('injection_points'))
{
	plan skip_all => 'Extension injection_points not installed';
}

$node->safe_psql('postgres', 'CREATE EXTENSION injection_points');

# Connect to the server and inject a waitpoint.
my $psql = $node->background_psql('postgres');
$psql->query_safe("SELECT injection_points_attach('init-pre-auth', 'wait')");

# From this point on, all new connections will hang during startup, just before
# authentication. Use the $psql connection handle for server interaction.
my $conn = $node->background_psql('postgres', wait => 0);

# Wait for the connection to show up in pg_stat_activity, with the wait_event
# of the injection point.
my $pid;
poll_until(
	sub {
		$pid = $psql->query(
			qq{SELECT pid FROM pg_stat_activity
  WHERE backend_type = 'client backend'
    AND state = 'starting'
    AND wait_event = 'init-pre-auth';});
		return $pid ne "";
	}) or die "timed out waiting for backend to reach init-pre-auth";

note "backend $pid is authenticating";
ok(1, 'authenticating connections are recorded in pg_stat_activity');

# Detach the waitpoint and wait for the connection to complete.
$psql->query_safe("SELECT injection_points_wakeup('init-pre-auth');");
$conn->wait_connect();

# Make sure the pgstat entry is updated eventually.
poll_until(
	sub {
		my $state =
		  $psql->query(
			"SELECT state FROM pg_stat_activity WHERE pid = $pid;");
		return 1 if $state eq "idle";

		note "state for backend $pid is '$state'; waiting for 'idle'...";
		return 0;
	}) or die "timed out waiting for backend $pid to become idle";

ok(1, 'authenticated connections reach idle state in pg_stat_activity');

$psql->query_safe("SELECT injection_points_detach('init-pre-auth');");
$psql->quit();
$conn->quit();

done_testing();
