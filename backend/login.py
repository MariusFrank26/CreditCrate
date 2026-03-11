import instaloader
import getpass

L = instaloader.Instaloader()
username = input("Instagram username: creditcrate.app")
password = getpass.getpass("Instagram password:Summerlove22..")
L.login(username, password)
L.save_session_to_file()
print("Session saved!")