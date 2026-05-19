from flask import Flask
import redis
import psycopg2
import os

app = Flask(__name__)

@app.route("/")
def home():
    return "Flask + PostgreSQL + Redis + NGINX funcionando"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)