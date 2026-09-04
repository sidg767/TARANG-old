import json
import time
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8001"


def test_health():
    print("\n=== BACKEND HEALTH TEST ===")

    try:
        start = time.time()

        with urllib.request.urlopen(
            BASE_URL + "/health",
            timeout=10
        ) as response:

            elapsed = time.time() - start
            body = response.read().decode("utf-8")

            print("Status Code:", response.status)
            print("Response Time:", round(elapsed, 2), "seconds")
            print("Response:", body)

            if response.status == 200:
                print("PASS: Backend is working.")
                return True
            else:
                print("FAIL: Backend returned non-200 status.")
                return False

    except Exception as e:
        print("FAIL: Cannot connect to backend.")
        print("Error:", e)
        return False


def test_copilot():
    print("\n=== AI COPILOT TEST ===")

    url = BASE_URL + "/api/copilot"

    data = json.dumps({
        "query": "Show salinity"
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        start = time.time()

        with urllib.request.urlopen(
            request,
            timeout=40
        ) as response:

            elapsed = time.time() - start
            body = response.read().decode("utf-8")

            print("Status Code:", response.status)
            print("Response Time:", round(elapsed, 2), "seconds")
            print("Response:", body)

            if response.status != 200:
                print("FAIL: Copilot returned non-200 status.")
                return False

            try:
                result = json.loads(body)
            except json.JSONDecodeError:
                print("FAIL: Response is not valid JSON.")
                return False

            if "answer" not in result:
                print("FAIL: JSON does not contain 'answer'.")
                return False

            print("PASS: AI Copilot is working and returned HTTP 200.")
            return True

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")

        print("Status Code:", e.code)
        print("Response:", body)
        print("FAIL: Copilot returned an HTTP error.")

        return False

    except urllib.error.URLError as e:
        print("FAIL: Could not connect to Copilot.")
        print("Error:", e.reason)

        return False

    except Exception as e:
        print("FAIL: Unexpected error.")
        print("Error:", e)

        return False


if __name__ == "__main__":

    health_ok = test_health()
    copilot_ok = test_copilot()

    print("\n==============================")
    print("FINAL RESULT")
    print("==============================")

    if health_ok and copilot_ok:
        print("PASS: TARANG backend and AI Copilot are working.")
    else:
        print("FAIL: Something is wrong with the backend or AI Copilot.")