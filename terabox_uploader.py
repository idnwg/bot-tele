import argparse
import os
import requests

def upload_file(connect_key, local_file, remote_path):
    # ini contoh, sesuaikan dengan lib/cli teraboxcli kamu
    cmd = f"teraboxcli --connect {connect_key} upload '{local_file}' '{remote_path}'"
    return os.system(cmd)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--connect", required=True)
    parser.add_argument("--file", help="file path")
    parser.add_argument("--dir", help="directory path")
    parser.add_argument("--dest", default="/")
    args = parser.parse_args()

    if args.file:
        upload_file(args.connect, args.file, args.dest)
    elif args.dir:
        for f in os.listdir(args.dir):
            local_file = os.path.join(args.dir, f)
            upload_file(args.connect, local_file, args.dest)
