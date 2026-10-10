import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.setup_db import alembic_config, ensure_database
from alembic import command
DB = "talent_os_main_autogen"
ensure_database(DB, reset=True)
cfg = alembic_config(DB)
command.upgrade(cfg, "20261001_901")
command.revision(cfg, message="autogen", autogenerate=True, rev_id="zz_autogen")
print("done")
