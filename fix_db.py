import psycopg2

conn = psycopg2.connect(
    "postgresql://replyhero_postgres_user:iMBJd6FQi862ZepggjwHB4bk8ISMnFtc@dpg-d84e66kvikkc7394065g-a.oregon-postgres.render.com/replyhero_postgres"
)

cur = conn.cursor()

cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS free_uses INTEGER DEFAULT 0;")
cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR DEFAULT 'free';")

conn.commit()
cur.close()
conn.close()

print('Database updated successfully!')