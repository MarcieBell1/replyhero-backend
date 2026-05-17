import requests

url = "http://127.0.0.1:5000/signup"
data = {
    "email": "test@test.com",
    "password": "123"
}

response = requests.post(url, json=data)
print("Status:", response.status_code)
print("Response:", response.text)