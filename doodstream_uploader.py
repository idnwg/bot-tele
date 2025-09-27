import argparse
import requests

API = "https://doodapi.com/api/upload/server"

def upload(api_key, file_path):
    # ambil server upload
    r = requests.get(API, params={"key": api_key})
    server = r.json()["result"]
    
    with open(file_path, "rb") as f:
        upload_url = server + "/upload/" + api_key
        r = requests.post(upload_url, files={"file": f})
        print(r.json())

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--file", required=True)
    args = parser.parse_args()
    upload(args.api, args.file)
