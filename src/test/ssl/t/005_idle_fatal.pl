
# Copyright (c) 2026, PostgreSQL Global Development Group

# Check that a FATAL error sent to an idle SSL connection reaches the client.
#
# On Windows, the client's TCP stack discards unread data when it receives a
# reset, so this depends on the backend closing its socket gracefully and on
# libpq reading pending input before sending its next query.  Doing that
# through TLS adds the close_notify exchange to the mix, so test it
# separately for each protocol version, as TLS 1.3 handles closure
# differently than TLS 1.2.

use strict;
use warnings FATAL => 'all';
use PostgreSQL::Test::Cluster;
use PostgreSQL::Test::Utils;
use Test::More;

use FindBin;
use lib $FindBin::RealBin;

use SSL::Server;

if ($ENV{with_ssl} ne 'openssl')
{
	plan skip_all => 'OpenSSL not supported by this build';
}
if (!$ENV{PG_TEST_EXTRA} || $ENV{PG_TEST_EXTRA} !~ /\bssl\b/)
{
	plan skip_all =>
	  'Potentially unsafe test SSL not enabled in PG_TEST_EXTRA';
}

my $ssl_server = SSL::Server->new();

my $SERVERHOSTADDR = '127.0.0.1';
my $SERVERHOSTCIDR = '127.0.0.1/32';

my $node = PostgreSQL::Test::Cluster->new('primary');
$node->init;

# PGHOST is enforced here to set up the node, subsequent connections
# will use a dedicated connection string.
$ENV{PGHOST} = $node->host;
$ENV{PGPORT} = $node->port;
$node->start;

$ssl_server->configure_test_server_for_ssl($node, $SERVERHOSTADDR,
	$SERVERHOSTCIDR, 'trust');
$ssl_server->switch_server_cert($node, certfile => 'server-cn-only');

# The pg_hba.conf set up above only allows SSL connections over TCP, so the
# session terminating the idle backend has to use SSL as well.
my $connstr =
	"sslkey=invalid sslcert=invalid sslrootcert=ssl/root+server_ca.crt "
  . "sslcrl=invalid sslcrldir=invalid sslmode=require dbname=trustdb "
  . "hostaddr=$SERVERHOSTADDR host=localhost user=ssltestuser";

foreach my $version ('TLSv1.2', 'TLSv1.3')
{
	my $version_connstr = "$connstr ssl_min_protocol_version=$version "
	  . "ssl_max_protocol_version=$version";

	my $timeout = IPC::Run::timer($PostgreSQL::Test::Utils::timeout_default);
	my ($stdin, $stdout, $stderr) = ('', '', '');
	my $psql = IPC::Run::start(
		[
			'psql', '--no-psqlrc', '--quiet', '--no-align', '--tuples-only',
			'--file' => '-',
			'--dbname' => $version_connstr
		],
		'<' => \$stdin,
		'>' => \$stdout,
		'2>' => \$stderr,
		$timeout);

	$stdin .=
	  "SELECT pid || ' ' || version FROM pg_stat_ssl WHERE pid = pg_backend_pid();\n";
	pump_until($psql, $timeout, \$stdout, qr/^\d+ TLS\S+[\r\n]/m);
	my ($pid, $used_version) = split ' ', $stdout;
	is($used_version, $version, "$version: connection uses $version");

	$node->safe_psql(
		'trustdb',
		"SELECT pg_terminate_backend($pid, 180000)",
		connstr => $connstr);

	$stdin .= "SELECT 1;\n";
	pump_until($psql, $timeout, \$stderr, qr/connection to server was lost/);
	$psql->finish;
	like(
		$stderr,
		qr/FATAL:  terminating connection due to administrator command/,
		"$version: FATAL while idle is reported");
}

done_testing();
