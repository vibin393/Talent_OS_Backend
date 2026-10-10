import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio, sys
import asyncpg
from app.core.settings import get_settings
async def main():
    s=get_settings()
    conn=await asyncpg.connect(user=s.db_user,password=s.db_password,host=s.db_host,port=s.db_port,database=sys.argv[1])
    rows=await conn.fetch(sys.argv[2])
    for r in rows: print(tuple(r))
    await conn.close()
asyncio.run(main())
