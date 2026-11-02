# baywatch

Finds business sellers on eBay and collects the business details they publish on their store's About tab (company, contact name, address, phone, email, VAT). It runs in slow motion so eBay doesn't notice.

It needs no eBay API key, developer account or message broker. It uses your installed Google Chrome and a single SQLite file.

## How it works

```
all categories ──► subcategories ──► leaf listings ──► item pages ──► seller store "About" tab ──► SQLite ──► CSV
```

0. **Categories (automatic):** starts at `/n/all-categories` (24 top-level, about 350 subcategories on ebay.it) and follows each subcategory's child links down to the leaves. A category whose page shows no new children is a leaf, and its listing pages (`max_pages`, with `listing_params`) are queued. Top-level categories are hub pages without pagination, so they aren't scraped themselves. Use `roots` to limit the crawl to a few top-level categories.
1. **Listing pages:** opens each listing page and collects the item links.
2. **Items:** opens each item once and records which store sells it. A seller found again through another item is never queued twice.
3. **Sellers:** opens the store's `?_tab=about` page and reads the business details block.

Several workers run in parallel (`workers`, default 4). Each one is a separate Chrome with its own profile in `.profile/workerN`. To eBay they look like separate visitors: tabs sharing one session got their page loads cancelled and drew far more error pages.

Everything is stored in `baywatch.db`, including the status and attempt count of every page, item and seller. `Ctrl+C` is always safe, and running again continues from where it stopped. The deepest pending stage always runs first, so finished sellers start appearing within the first minute.

In a test run, 4 workers found about 280 new sellers per hour.

## Setup

Needs Python 3.11+ and Google Chrome.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (on macOS/Linux: source .venv/bin/activate)
pip install -e .                  # core
pip install -e .[solver]          # optional: automatic captcha solver (torch + CLIP, a few GB)
copy config.example.toml config.toml
```

Edit `config.toml` if you want to. The defaults crawl every category on ebay.it. The settings you're most likely to change:
- **`[discovery] roots`:** restrict the crawl to some top-level categories.
- **`seller_location`:** which country's sellers to keep.
- **`workers`:** how many browsers run in parallel.
- **`captcha_solver`:** automatic or manual captcha solving.

## Usage

```bash
baywatch run                      # go (Ctrl+C to stop, run again to resume)
baywatch run --max-sellers 50     # stop after 50 sellers
baywatch run -w 6                 # 6 parallel workers (overrides config)
baywatch -v run                   # verbose: every item, refresh and redirect
baywatch status                   # progress counts
baywatch categories               # discovered categories, leaves and listing progress per top-level category
baywatch export -o sellers.csv    # sellers with business info, located in seller_location
baywatch export --all             # every seller, with a Status column
baywatch retry-failed             # requeue anything that ran out of attempts
baywatch import-csv old.csv       # import a scrapita ebay_sellers.csv so those sellers are skipped
```

## Captchas and blocks

| What you see | What baywatch does |
|---|---|
| `Error Page \| eBay`, "Sorry, something went wrong on our end" | This is an eBay bug that regular users hit too, not a block, so baywatch just refreshes (up to `error_refreshes` times). In testing, every one cleared this way. |
| `net::ERR_ABORTED` on a page load | Chrome dropped the navigation. baywatch retries after a short pause. |
| Listings that never load (skeleton cards, "Si è verificato un errore") | Counted as a block. After `free_retries` in a row, that worker pauses (10s, 20s, 40s… up to 2 min) while the others keep going. |
| hCaptcha ("Procedi alla verifica per continuare") | With `captcha_solver = "clip"`, the automatic solver clicks the checkbox and solves image challenges with CLIP. If that fails, the window comes to the front and waits for you to solve it by hand (up to `captcha_timeout`). Only one captcha is handled at a time. |

Each worker's profile keeps its cookies between runs, so a solved captcha stays solved for a while.

Practical tips:
- **Use installed Chrome.** Leave `browser_channel = "chrome"`. Playwright's bundled Chromium gets blocked much sooner.
- **Expect errors from headless mode.** It's off on purpose, because eBay serves error pages to headless Chrome.
- **Automation flags are off.** Playwright's `--enable-automation` and `--no-sandbox` flags are removed, so you won't see the "unsupported command-line flag" banner.

## Output columns

`Location, Store Link, Company Name, First Name, Last Name, Address, Phone, Fax, Email, VAT, Further Info, Status, Scraped At`

How values are cleaned:
- **Phone and fax:** normalised to international format (`+393331234567`).
- **VAT:** compacted (`IT01234567890`). When a seller lists several, the one for your country is chosen.
- **Email:** text that isn't a valid address is moved to *Further Info*.

## Other eBay sites

Set the `site` and `phone_region`. Category discovery works the same way on every eBay site that has `/n/all-categories`. Then add that site's About-tab labels under `[labels]` in the config: the Italian labels ship by default, and the others are up to you.

## Tests

```bash
pip install -e .[dev]
pytest
```

## Notes

- **Stores only:** only sellers with an eBay store (`/str/...`) are collected. Items from sellers without a store are recorded as `no_store`.
- **Personal data:** contact names, emails and phone numbers of EU sole traders are personal data under GDPR. If you hand this data to someone else, keep track of where it came from (`Store Link`, `Scraped At` are there for that), and honour removal requests.
- **eBay's terms:** eBay's User Agreement restricts automated access. You are responsible for how you use this tool.

## License

MIT, see [LICENSE](LICENSE).
