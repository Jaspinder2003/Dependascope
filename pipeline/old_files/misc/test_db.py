import sys
sys.path.insert(0, r'c:\Users\jaspi\OneDrive\Desktop\dependabot-failing\pipeline')
import config as C
import db

# Init DB
conn = db.init_db(C.DB_PATH)
print('DB initialized at:', C.DB_PATH)

# Verify tables
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables:', [t[0] for t in tables])
conn.close()
print('PASS: DB module OK')
