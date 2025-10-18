
# Copyright (c) 2021-2026, PostgreSQL Global Development Group

use strict;
use warnings FATAL => 'all';

use PostgreSQL::Test::Cluster;
use PostgreSQL::Test::Utils;
use Test::More;
use Time::HiRes qw(usleep);

my $node = PostgreSQL::Test::Cluster->new('main');
$node->init;
$node->start;

my $psql = $node->background_psql('postgres');

# Confirm connection works
my $result = $psql->query_safe("SELECT 'before_goaway'");
like($result, qr/before_goaway/, 'connection works before smart shutdown');

# Initiate smart shutdown without waiting for it to complete
$node->command_ok(
	[ 'pg_ctl', 'stop', '-D', $node->data_dir, '-m', 'smart', '--no-wait' ],
	'pg_ctl smart shutdown');

# The backend sends GoAway once it processes the smart shutdown signal.
# Poll with queries until psql reports it.
my $saw_goaway = 0;
for (my $i = 0; $i < 100; $i++)
{
	my $out = $psql->query("SELECT 'after_goaway'");
	if ($psql->{stderr} =~ /Server sent GoAway, requesting disconnect when convenient/)
	{
		$saw_goaway = 1;
		# The query should still have succeeded
		like($out, qr/after_goaway/, 'query still works after GoAway');
		last;
	}
	usleep(50_000);
}
ok($saw_goaway, 'psql reported GoAway notice during smart shutdown');

$psql->quit;

done_testing();
