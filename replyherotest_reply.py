import requests

session = requests.Session()

# 1. Login or signup first
signup_url = "http://127.0.0.1:5000/signup"
signup_data = {
    "email": "test@test.com",
    "password": "123"
}
session.post(signup_url, json=signup_data)

# 2. Now call /reply using the same session (cookies included)
reply_url = "http://127.0.0.1:5000/reply"
reply_data = {
    "message": "hello"
}

response = session.post(reply_url, json=reply_data)
print("Status:", response.status_code)
print("Response:", response.text)