import os
import instaloader
import getpass

# Einmalig ausführen, um die Instagram-Session zu erzeugen, die app.py danach
# wiederverwendet. Zugangsdaten werden NICHT im Code gespeichert - nur einmal
# manuell eingegeben und dann als Session-File auf der Platte abgelegt.
default_username = os.getenv("IG_SESSION_USERNAME", "")

L = instaloader.Instaloader()
username = input(f"Instagram username [{default_username or 'z.B. creditcrate.app'}]: ").strip() or default_username
password = getpass.getpass("Instagram password: ")
L.login(username, password)
L.save_session_to_file()
print(f"Session für @{username} gespeichert!")
