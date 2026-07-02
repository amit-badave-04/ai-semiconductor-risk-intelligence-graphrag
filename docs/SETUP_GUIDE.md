# Beginner Setup Guide — Neo4j, Anthropic API Key, and Running Notebooks in VS Code

Three one-time setups, ~20 minutes total. After this, notebooks 00 and 05–11 will run.

---

## Part 1 — Anthropic API key (~5 min, needs a card)

**Important: your Claude Max plan does NOT include API access.** Max covers the Claude apps and
Claude Code. The notebooks call Claude through **the Anthropic API**, which is a separate,
pay-as-you-go product billed by tokens. You need a (free) developer account plus a small prepaid
credit purchase. The whole Nvidia PoC extraction is estimated at **$2–3 of API usage**, so the
$5 minimum credit purchase covers it.

1. Go to **https://platform.claude.com/** (the Anthropic Console) and sign in — you can use the
   same email as your Claude account; the Console account is still separate billing-wise.
2. First-time setup will ask you to create an **organization** — accept the defaults.
3. Open **Settings → Billing** (left sidebar) → **Buy credits** → add **$5** (minimum). This is
   prepaid credit, not a subscription — it only depletes as you make API calls.
4. Open **Settings → API keys** → **Create key**. Name it something like `graphrag-notebooks`.
5. The key (starts with `sk-ant-`) is **shown only once** — copy it immediately.
6. Open the file **`.env`** in the project root (it already exists; it's hidden from git) and paste
   the key so the line reads:

   ```
   ANTHROPIC_API_KEY=sk-ant-api03-...your-key...
   ```

   No quotes, no spaces around `=`. Save the file.

> Safety notes: never commit `.env` (it's already in `.gitignore`); never paste the key into chat
> or code. If a key leaks, delete it in the Console and create a new one.
> You can watch your spend at Console → Usage.

## Part 2 — Neo4j Desktop (~10 min)

Neo4j is the graph database where the knowledge graph lives. "Desktop" is a free GUI app that runs
the database locally on your PC — no server, no account fees.

1. Download: **https://neo4j.com/download/** → "Download Neo4j Desktop" (Windows). It asks for
   name/email and shows an **activation key** on the download page — copy it; the installer asks
   for it on first launch.
2. Run the installer, launch **Neo4j Desktop**, paste the activation key if prompted.
3. Create the database instance:
   - You'll see a default **Project** (or create one: *+ New Project*).
   - Inside the project click **Add ▾ → Local DBMS**.
   - Name: `graphrag` (anything works). Version: pick the latest **5.x**.
   - **Set a password** — remember it, you'll need it in step 5. (Username is always `neo4j`.)
   - Click **Create**.
4. Click **Start** on the new DBMS and wait until its status turns green/“Active”.
   ⚠️ The database only runs while Neo4j Desktop has it started — before any notebook session,
   open Neo4j Desktop and press Start.
5. Put the password into **`.env`**:

   ```
   NEO4J_URI=bolt://localhost:7687
   NEO4J_USER=neo4j
   NEO4J_PASSWORD=the-password-you-chose
   ```

   The URI and user lines are already correct in the file — only the password needs editing.
6. Optional sanity check: click **Open** on the DBMS — the Neo4j Browser opens; type `RETURN 1`
   in the top command bar and press ▶. If you get a `1` back, the database works.

## Part 3 — Running the notebooks in VS Code (~5 min)

The project's Python environment lives in `.venv/` (created by uv) and is registered as a named
Jupyter kernel: **`Python (ai-semiconductor-risk-intelligence-graphrag)`**.

1. In VS Code, install the **Python** and **Jupyter** extensions (Microsoft) if you don't have them.
2. **File → Open Folder** → this project folder.
3. Open a notebook, e.g. `notebooks/00_smoke_test.ipynb`.
4. Click **Select Kernel** (top-right of the notebook) → **Jupyter Kernel...** →
   pick **`Python (ai-semiconductor-risk-intelligence-graphrag)`**.
   (Alternative that works equally well: choose *Python Environments* → `.venv` in this project.)
5. You only need to pick the kernel once per notebook — VS Code remembers it.

### Run order and what to expect

Run each notebook top-to-bottom (toolbar: **Run All**; clean re-test: **Restart** then **Run All**).
Each ends with an assertion cell that prints an `... OK` line — if you see it, the notebook passed.

| # | Notebook | Needs | Notes |
|---|---|---|---|
| 0 | `00_smoke_test` | Neo4j started + API key | First run downloads the ~1.3 GB embedding model (one-time, needs disk + patience) |
| 1 | `05_graph_schema` | Neo4j | Creates constraints & vector indexes |
| 2 | `06_deterministic_layer` | Neo4j | Loads companies/filings/metrics — no LLM |
| 3 | `07_llm_extraction` | API key | **The paid step, ~$2–3.** Checkpointed: safe to interrupt and re-run, it resumes |
| 4 | `08_entity_resolution` | — | Pure Python |
| 5 | `09_embeddings_and_evidence` | Neo4j | Embeds 439 chunks on CPU (~1–2 min), loads the knowledge |
| 6 | `10_retrieval_strategies` | Neo4j + API key | Compares 3 retrieval modes |
| 7 | `11_answer_generation` | Neo4j + API key | Final PoC: cited answers |

(Notebooks 01–04 are already executed — their outputs are committed. Re-running them is harmless.)

### If something fails

- **`Neo4j is not reachable`** → Neo4j Desktop isn't running or the DBMS isn't started, or the
  password in `.env` is wrong.
- **`ANTHROPIC_API_KEY is empty`** → Part 1 step 6 wasn't saved, or you edited `.env.example`
  instead of `.env`.
- **401 authentication error from the API** → key copied incompletely, or no credits purchased.
- **Kernel not in the list** → run in a terminal:
  `uv run python -m ipykernel install --user --name ai-semiconductor-risk-intelligence-graphrag --display-name "Python (ai-semiconductor-risk-intelligence-graphrag)"`
