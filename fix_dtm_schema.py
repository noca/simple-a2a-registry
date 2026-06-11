#!/usr/bin/env python3
"""Fix triple-quoted schema strings in dtm.py"""
import base64

with open("simple_a2a_registry/security/dtm.py", "rb") as f:
    content = f.read()

# The schema text with proper triple quotes
# We use bytes to avoid any escaping issues
schema_sqlite = b'"""\nCREATE TABLE IF NOT EXISTS delegation_tokens (\n    jti          TEXT PRIMARY KEY,\n    task_id      TEXT NOT NULL,\n    sub          TEXT NOT NULL,\n    origin_agent TEXT NOT NULL,\n    scope        TEXT NOT NULL,\n    depth        INTEGER NOT NULL DEFAULT 0,\n    expires_at   INTEGER NOT NULL,\n    used_at      TIMESTAMP,\n    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n    FOREIGN KEY (task_id) REFERENCES tasks(id)\n);\n"""'

schema_mysql = b'"""\nCREATE TABLE IF NOT EXISTS delegation_tokens (\n    jti          VARCHAR(64) PRIMARY KEY,\n    task_id      VARCHAR(64) NOT NULL,\n    sub          VARCHAR(255) NOT NULL,\n    origin_agent VARCHAR(255) NOT NULL,\n    scope        VARCHAR(255) NOT NULL,\n    depth        INT NOT NULL DEFAULT 0,\n    expires_at   BIGINT NOT NULL,\n    used_at      DOUBLE,\n    created_at   DOUBLE\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n"""'

# Replace the broken "***" pattern
old_line1 = b'DELEGATION_TOKENS_SCHEMA="***"\n'
new_line1 = b'DELEGATION_TOKENS_SCHEMA=' + schema_sqlite + b'\n'

old_line2 = b'DELEGATION_TOKENS_SCHEMA_MYSQL="***"\n'
new_line2 = b'DELEGATION_TOKENS_SCHEMA_MYSQL=' + schema_mysql + b'\n'

if old_line1 in content:
    content = content.replace(old_line1, new_line1, 1)
    print("Fixed DELEGATION_TOKENS_SCHEMA")
else:
    print("DELEGATION_TOKENS_SCHEMA not found as expected")

if old_line2 in content:
    content = content.replace(old_line2, new_line2, 1)
    print("Fixed DELEGATION_TOKENS_SCHEMA_MYSQL")
else:
    print("DELEGATION_TOKENS_SCHEMA_MYSQL not found as expected")

with open("simple_a2a_registry/security/dtm.py", "wb") as f:
    f.write(content)

import ast
try:
    ast.parse(content.decode("utf-8"))
    print("SYNTAX OK")
except SyntaxError as e:
    print(f"Syntax error: {e}")