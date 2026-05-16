from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Hello Marcie — your Render app is running!"