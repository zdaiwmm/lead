"""The restored DB uses actual restricted roles, without changing production HBA."""
import importlib.util
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from lead_app.config import database_kwargs
from psycopg import sql
from psycopg_pool import ConnectionPool


def helper():
    path=Path(__file__).parents[1]/'scripts/growth_runtime_upgrade.py'
    spec=importlib.util.spec_from_file_location('growth_drill',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operator_uses_the_registered_site_owner_instead_of_a_development_default(tmp_path,monkeypatch):
    upgrade=helper()
    settings={'PGDATABASE':'lead','PGHOST':str(tmp_path/'pgsock'),'PGPORT':'25434',
        'PGUSER':'synthetic_site_owner','PGPASSWORD':'fixture-only'}
    with monkeypatch.context() as patch:
        for key,value in upgrade.database_environment(tmp_path,settings).items():
            patch.setenv(key,value)
        selected=database_kwargs(owner=True)
        assert selected['user']==settings['PGUSER'] and selected['password']==settings['PGPASSWORD']
        assert selected['host']==settings['PGHOST'] and selected['dbname']=='lead'
    with pytest.raises(ValueError,match='Unexpected database target'):
        upgrade.database_environment(tmp_path,{**settings,'PGDATABASE':'unrelated'})


def test_restored_database_role_transport_enforces_real_rls_and_restores_connections():
    upgrade=helper()
    name='lead_growth_drill_'+uuid4().hex[:12]
    owner=database_kwargs(owner=True)
    original=psycopg.connect
    with psycopg.connect(**{**owner,'dbname':'postgres'},autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
        try:
            admin.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
            with psycopg.connect(**{**owner,'dbname':name}) as c:
                c.execute('CREATE TABLE public.drill_private_fact(value text)')
                c.execute("INSERT INTO public.drill_private_fact VALUES('synthetic-private-row')")
                c.execute('GRANT SELECT ON public.drill_private_fact TO lead_runtime')
                c.execute('ALTER TABLE public.drill_private_fact ENABLE ROW LEVEL SECURITY')
                c.execute('ALTER TABLE public.drill_private_fact FORCE ROW LEVEL SECURITY')
                c.execute('CREATE POLICY no_rows ON public.drill_private_fact TO lead_runtime USING(false)')
            with upgrade.drill_role_connections(name,owner):
                for role in ('lead_auth','lead_runtime'):
                    kwargs={**owner,'dbname':name,'user':role,'password':'deliberately-not-a-valid-login'}
                    with psycopg.connect(**kwargs) as c:
                        assert c.info.transaction_status==psycopg.pq.TransactionStatus.IDLE
                        assert c.execute('SELECT current_user').fetchone()[0]==role
                        if role=='lead_runtime':
                            assert c.execute('SELECT * FROM public.drill_private_fact').fetchall()==[]
                        else:
                            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                                c.execute('SELECT * FROM public.drill_private_fact')
                    with ConnectionPool('',kwargs=kwargs,min_size=1,max_size=1) as pool:
                        with pool.connection() as c:
                            assert c.execute('SELECT current_user').fetchone()[0]==role
            assert psycopg.connect is original
            with pytest.raises(ValueError,match='isolated drill'):
                with upgrade.drill_role_connections('lead',owner):
                    pytest.fail('The production database cannot use the rehearsal transport')
        finally:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
