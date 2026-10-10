import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.setup_db import alembic_config
from alembic import command
DB = "talent_os_main_autogen"
cfg = alembic_config(DB)
for step in sys.argv[1:]:
    if step == "check":
        command.check(cfg)
    elif step.startswith("down:"):
        command.downgrade(cfg, step[5:])
    else:
        command.upgrade(cfg, step)
    print("OK", step)
