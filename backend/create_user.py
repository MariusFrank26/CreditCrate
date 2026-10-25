"""
Creates a CreditCrate account, or resets the password of an existing one.
Sign-up is closed, so run this on the server:  python create_user.py
"""

import getpass
import accounts

accounts.init_db()
email = input("Account email: ").strip()
password = getpass.getpass("Password: ")
accounts.create_user(email, password)
print(f"Account {accounts.normalize_email(email)} saved.")
