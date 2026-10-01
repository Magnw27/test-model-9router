# OmniRoute Model Tester & Benchmark

An automatic Python toolkit for scanning, testing availability, measuring latency (*benchmark*), and verifying *tool-calling* (function calling) capabilities of all AI models connected to an account on [OmniRoute](https://omniroute.ai).

Very useful before creating *Combo* / *Fallback List* configurations in OmniRoute, so you can know exactly which models are actually active, stable, fast, and ready to be used in your application pipeline or AI agent.

---

## 🌟 Main Features

1. **Realistic Speed Classification**:

   * `FAST`: Response $\le 15$ seconds (configurable).
   * `NORMAL`: Response $15$ – $45$ seconds (configurable).
   * `SLOW`: Response $> 45$ seconds.
   * `TIMEOUT`: More than 120 seconds (considered failed).

2. **Smart Error Classification (Permanent vs Temporary)**:

   * Distinguishes permanent errors (`BLOCKED`) such as depleted credits or retired models from temporary issues (`TRANSIENT`) such as rate limits.

3. **Anti-Rate Limit & Provider Protection**:

   * **Interleaving**: Requests are alternated between providers (instead of bombarding the same provider consecutively).
   * **Automatic Delay & Retry**: Automatically reads the `reset after ...s` delay when a rate limit is encountered.
   * **Circuit Breaker**: Automatically pauses testing for a provider if that provider repeatedly encounters rate limits.

4. **Tool Calling Verification (`--tools`)**:

   * Tests whether a model actually supports structured *function calling* (OpenAI-compatible tool calls), rather than simply responding with ordinary text.

5. **Safe Status Storage (*Resumable*)**:

   * Test results are stored atomically in `test_state.json` every time one model finishes testing. If the connection is lost or the laptop shuts down, testing can continue immediately without starting over from the beginning.

---

## 📁 File Structure

```text
├── list_models.py       # Retrieves all active Model IDs from OmniRoute
├── llm_client.py        # Model calling wrapper (LangChain ChatOpenAI via OmniRoute)
├── test_all_models.py   # Main testing script (parallel testing, benchmarking, & analysis)
├── requirements.txt     # List of required Python libraries
├── .env.example         # URL and API Key configuration template
├── .gitignore           # Keeps sensitive files (.env) and result logs from being uploaded
└── README.md            # Complete documentation & usage guide
```

---

## 🧭 Usage Order (Quick Workflow)

To ensure the testing process runs smoothly without issues, follow this order:

```text
[1. Prepare Python & Dependencies] ---> [2. Configure the .env File]

                  │                              │
                  ▼                              ▼

[3. Make Sure OmniRoute Is Running] ---> [4. Run list_models.py]
                                               │
                                               ▼
                                    [5. Run test_all_models.py]
                                               │
                                               ▼
                                    [6. Open test_results.txt]
```

1. **Environment Setup**: Create a virtual environment and install the libraries from `requirements.txt`.

2. **Credential Configuration**: Copy `.env.example` to `.env` and enter your OmniRoute API Key.

3. **Start OmniRoute**: Make sure the local OmniRoute application or server is running.

4. **Retrieve Model List**: Run `python list_models.py` to download the latest model list into `all_models.txt`.

5. **Run Testing**: Run `python test_all_models.py` to start the testing process.

6. **Analyze Results**: Read the summary in `test_results.txt` or check the JSON data in `test_state.json`.

---

## 📋 Requirements Before Running

1. **Python 3.10 or newer** ([Download Python](https://www.python.org/downloads/)).

2. **OmniRoute application** is running on your local computer or server.

3. **Your own OmniRoute API Key** (can be found in the OmniRoute dashboard).

---

## 🚀 Step-by-Step Usage Guide

### Step 1: Set Up Virtual Environment & Install Dependencies

Open a terminal inside this folder, then run:

```bash
# Create virtual environment

python -m venv venv

# Activate

# On Windows (Command Prompt / PowerShell):

venv\Scripts\activate

# On macOS / Linux:

source venv/bin/activate

# Install dependencies

pip install -r requirements.txt
```

### Step 2: Configure API Key (`.env`)

Copy `.env.example` to `.env`:

```bash
# Windows

copy .env.example .env

# macOS / Linux:

cp .env.example .env
```

Open the `.env` file with a text editor, then adjust the URL and API key:

```env
OMNIROUTE_BASE_URL=http://localhost:20128/v1
OMNIROUTE_API_KEY=enter_your_omniroute_api_key_here
```

> ⚠️ **SECURITY WARNING**: Never share or upload the `.env` file publicly/to Git because it contains your personal API key.

### Step 3: Retrieve the Latest Model List

Run this command to retrieve all models available in your OmniRoute account:

```bash
python list_models.py
```

*This script will create the `all_models.txt` file containing the list of all connected Model IDs.*

### Step 4: Run Model Testing

Choose the testing command according to your needs:

```bash
# Test new models that have never been tested + retry models that previously failed temporarily

python test_all_models.py

# Re-test ALL models from the beginning (adds model stability records)

python test_all_models.py --all

# Add Tool-Calling (Function Calling) capability testing

python test_all_models.py --tools

# Test only specific providers (example: google and anthropic)

python test_all_models.py --only google,anthropic

# Skip specific providers (example: skip openrouter and groq)

python test_all_models.py --skip openrouter,groq

# Set the number of parallel workers (default: 8)

python test_all_models.py --workers 4
```

After completion, you can open **`test_results.txt`** to view a clean text summary, or view the complete data in **`test_state.json`**.

---

## ⚙️ Parameter Customization & Configuration

If you want to adjust speed thresholds, maximum waiting time, delays between requests, or retry criteria, you can modify the configuration variables at the top of **`test_all_models.py`** (around lines 48–60):

```python
# ---------------------------------------------------------------- ADJUSTABLE SETTINGS

PROMPT = "Reply with only one word: OK"

# 1. LATENCY SPEED THRESHOLD SETTINGS

FAST_MAX = 15.0      # Threshold (seconds) for the FAST category. Responses <= 15s are classified as FAST.

NORMAL_MAX = 45.0    # Threshold (seconds) for the NORMAL category. Responses 15s - 45s are classified as NORMAL.

                     # Above NORMAL_MAX, the response is classified as SLOW.

# 2. MAXIMUM TIMEOUT LIMIT

HARD_LIMIT = 120.0   # Maximum response waiting time (seconds). If the model does not respond within

                     # this limit, the request is terminated and considered TIMEOUT (failed).

                     # You can increase it (for example, to 180 or 300) when testing

                     # heavy reasoning models that need a long thinking time.

# 3. RETRY & CIRCUIT BREAKER SETTINGS

MAX_ATTEMPTS = 3     # Number of retry attempts for TRANSIENT errors (429/server busy).

BACKOFF = [5.0, 15.0, 30.0]  # Waiting time (seconds) for attempts 1, 2, and 3

                              # if the server does not provide a specific "reset after" instruction.

RETRY_CAP = 45.0     # Maximum waiting time between retry attempts (seconds).

BREAKER_LIMIT = 8    # Circuit Breaker: if one provider receives rate limits consecutively

                     # this many times, the remaining models from that provider are temporarily skipped.

# 4. DELAY BETWEEN REQUESTS (ANTI-RATE LIMIT)

DEFAULT_GAP = 1.0    # Rest period (seconds) after each request to the same provider.

PROVIDER_GAP = {"af": 2.0}  # Special delay for providers with strict limits

                            # (for example, provider 'af' is limited to 1 request every 2 seconds).

# 5. MODELS OR COMBOS TO IGNORE (OPTIONAL)

KNOWN_COMBOS = set([
    # "model-id-or-combo-to-skip",
])
```

---

## 🔍 Explanation of Test Result Statuses (Why Are There Statuses Other Than OK?)

This script is designed not merely to say "failed", but to analyze the actual cause of the provider response. Here is the meaning of each status:

### 1. `OK` Status

The model is active, healthy, and successfully responded to the test message `"Reply with only one word: OK"`.

`OK` models are grouped according to their speed:

* **`FAST`** ($\le FAST_MAX$): Very suitable for *real-time* tasks or interactive chatbots.
* **`NORMAL`** ($FAST_MAX - NORMAL_MAX$): Suitable for general data processing or background tasks.
* **`SLOW`** ($> NORMAL_MAX$): The model is slow or is experiencing a high server queue.
* *(Stability note: If tested again and continues to succeed, it will be marked as `stable(2x OK)`, etc.).*

### 2. `BLOCKED` Status (Permanent Failure)

The model failed and **will not be usable** without corrective action on your side or from the provider. The script analyzes the failure reason (`reason`):

* **`no_credit`**: The provider's account balance / API quota is depleted, or the model requires a paid subscription (HTTP 402).
* **`auth`**: The API key is incorrect, the token format is invalid, or the account has not been authenticated with that provider (HTTP 401 / 403).
* **`retired`**: The model has officially been shut down / retired (*End of Life*) by its original creator (HTTP 410 "Gone").
* **`invalid_model`**: The Model ID is misspelled, unsupported, or your account does not have access permission (HTTP 404).
* **`incompatible`**: The model is not a standard chat completions type (for example, a classification / decision model that requires a specialized endpoint).
* **`unavailable`**: All provider routes/channels for the model have been completely disabled.

### 3. `TRANSIENT` Status (Temporary Issue)

The model failed because of a temporary issue, **not because the model itself is broken**.

* **Cause**: A *Rate Limit* was encountered (HTTP 429 "Too Many Requests"), the requests-per-minute (RPM) quota was full, or the provider server was overloaded (HTTP 500, 502, 503).
* **Automatic Solution**: The script automatically detects the `reset after ...` header, waits using *exponential backoff*, and retries up to 3 times. If it still fails, you only need to run `python test_all_models.py` again after some time.

### 4. `THROTTLED_SKIP` Status (Circuit Breaker)

If a provider continuously returns 429 / TRANSIENT errors more than 8 consecutive times, the script automatically activates the *Circuit Breaker* to skip the remaining models from that provider. The purpose is to prevent your account from being put into a *cooldown* / *IP ban* by the provider.

### 5. `EMPTY` Status

The request was successfully sent and the server responded with status 200 OK, **but the model response content was completely empty** (an empty string). This indicates that the model experienced a *silent failure* during inference.

### 6. `TIMEOUT` Status

The model takes longer than `HARD_LIMIT` (default 120 seconds) to respond, so the request is terminated by the script to prevent the process from getting stuck indefinitely.

---

## 🛠️ Tool-Calling Testing (`--tools`)

If you include the `--tools` parameter, the script will test whether models with an `OK` status have **Function / Tool Calling** capabilities.

### What Is Tested?

The script registers a *dummy tool* using LangChain:

```python
@tool

def get_time(city: str) -> str:
    """Get the current time for a city."""
    return "12:00"
```

The model is then instructed:

*"Use the get_time tool for the city Jakarta."*

### How Does It Work?

* **Normal Model / Failure**: The model will only respond with ordinary text (for example: *"The time in Jakarta is 12:00"*), or throw an error because it does not understand the JSON schema for tool calling. Result: `tools=NO` or `tools=error`.
* **Tool-Capable Model**: The model will not immediately respond with text. Instead, it returns a structured `tool_calls` payload instructing the application to execute the `get_time(city='Jakarta')` function. Result: `tools=YES`.

> 💡 **Why is this important?**
>
> If you are building a Multi-Agent system (for example, a Customer Service bot that needs to retrieve database data, check ticket status, or call an external API), make sure you only select models labeled `tools=YES`.

---

## 📊 Generated Files

After testing has run, you will see these files in the directory:

1. **`all_models.txt`**: Raw list of all models retrieved from OmniRoute.
2. **`test_state.json`**: Complete JSON-formatted status database. This file stores latency, original error history, test timestamps, and tool capabilities.
3. **`test_results.txt`**: A concise report that separates models by status and speed.

---

## ⚖️ License

This project is licensed under the **MIT** license. You are free to use, modify, and redistribute it.
