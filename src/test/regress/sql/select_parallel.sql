--
-- PARALLEL
--

-- Save parallel worker stats, used for comparison at the end
select pg_stat_force_next_flush();
select parallel_workers_to_launch as parallel_workers_to_launch_before,
       parallel_workers_launched as parallel_workers_launched_before
  from pg_stat_database
  where datname = current_database() \gset

create function sp_parallel_restricted(int) returns int as
  $$begin return $1; end$$ language plpgsql parallel restricted;

begin;

-- encourage use of parallel plans
set parallel_setup_cost=0;
set parallel_tuple_cost=0;
set min_parallel_table_scan_size=0;
set max_parallel_workers_per_gather=4;

-- Parallel Append with partial-subplans
explain (costs off)
  select round(avg(aa)), sum(aa) from a_star;
select round(avg(aa)), sum(aa) from a_star a1;

-- Parallel Append with both partial and non-partial subplans
alter table c_star set (parallel_workers = 0);
alter table d_star set (parallel_workers = 0);
explain (costs off)
  select round(avg(aa)), sum(aa) from a_star;
select round(avg(aa)), sum(aa) from a_star a2;

-- Parallel Append with only non-partial subplans
alter table a_star set (parallel_workers = 0);
alter table b_star set (parallel_workers = 0);
alter table e_star set (parallel_workers = 0);
alter table f_star set (parallel_workers = 0);
explain (costs off)
  select round(avg(aa)), sum(aa) from a_star;
select round(avg(aa)), sum(aa) from a_star a3;

-- Disable Parallel Append
alter table a_star reset (parallel_workers);
alter table b_star reset (parallel_workers);
alter table c_star reset (parallel_workers);
alter table d_star reset (parallel_workers);
alter table e_star reset (parallel_workers);
alter table f_star reset (parallel_workers);
set enable_parallel_append to off;
explain (costs off)
  select round(avg(aa)), sum(aa) from a_star;
select round(avg(aa)), sum(aa) from a_star a4;
reset enable_parallel_append;
