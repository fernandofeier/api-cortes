
import subprocess
import sys
import os

def create_dummy_video(filename="dummy.mp4"):
    # Create a 10s video with audio
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=10",
        "-c:v", "libx264", "-c:a", "aac", "-shortest",
        filename
    ]
    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    if not os.path.exists("dummy.mp4"):
        print("Creating dummy video...")
        create_dummy_video()
    else:
        print("Dummy video exists.")
