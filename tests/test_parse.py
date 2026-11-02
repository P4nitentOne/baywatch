from baywatch import parse

SITE = "https://www.ebay.it"
LABELS = {
    "company": ("ragione sociale",),
    "first_name": ("nome",),
    "last_name": ("cognome",),
    "address": ("indirizzo",),
    "phone": ("numero di telefono",),
    "fax": ("numero di fax",),
    "email": ("email", "e-mail"),
    "vat": ("partita iva",),
    "further_info": ("altre informazioni",),
}


def test_item_urls_dedupes_and_strips_tracking():
    hrefs = [
        "https://www.ebay.it/itm/125185543800?itmmeta=abc&hash=item1d25",
        "https://www.ebay.it/itm/125185543800?_trksid=xyz",
        "https://www.ebay.it/itm/Some-Title-Slug/266123456789?var=1",
        "https://www.ebay.it/b/not-an-item/123",
    ]
    assert parse.item_urls(hrefs, SITE) == {
        "125185543800": "https://www.ebay.it/itm/125185543800",
        "266123456789": "https://www.ebay.it/itm/266123456789",
    }


def test_store_url_is_canonical():
    hrefs = [
        "https://www.ebay.it/usr/someone",
        "https://www.ebay.it/str/ExampleStore?_trksid=p4429486.m145687.l149266",
        "https://www.ebay.it/str/examplestore/Some-Category/_i.html",
    ]
    assert parse.store_url(hrefs, SITE) == "https://www.ebay.it/str/examplestore"
    assert parse.store_url(["https://www.ebay.it/usr/x"], SITE) is None


def test_match_fields_pairs_labels_and_values():
    texts = [
        "Ragione sociale:", "Esempio s. r. l.",
        "Nome:", "MARIO",
        "Cognome:", "BIANCHI",
        "Indirizzo:", "Via Roma 1, MILANO, MI, Italy 20100",
        "Numero di telefono:", "+39 3331234567",
        "Email:", "info@esempio.it",
        "Partita IVA:", "IT 01234567890",
    ]
    assert parse.match_fields(texts, LABELS) == {
        "company": "Esempio s. r. l.",
        "first_name": "MARIO",
        "last_name": "BIANCHI",
        "address": "Via Roma 1, MILANO, MI, Italy 20100",
        "phone": "+39 3331234567",
        "email": "info@esempio.it",
        "vat": "IT 01234567890",
    }


def test_match_fields_nome_does_not_claim_cognome():
    assert parse.match_fields(["Cognome", "ROSSI"], LABELS) == {"last_name": "ROSSI"}


def test_match_fields_skips_empty_values_without_shifting():
    # An empty email must not make "Partita IVA" the email's value.
    texts = ["Email:", "", "Partita IVA:", "IT 01234567890"]
    assert parse.match_fields(texts, LABELS) == {"vat": "IT 01234567890"}


def test_location():
    assert parse.location(["Iscritto dal 2010", "Luogo:\xa0Italia"]) == "Italia"
    assert parse.location(["Location: Germany"]) == "Germany"
    assert parse.location(["nothing here"]) is None


def test_phone_normalisation():
    assert parse.phone("+39 3331234567", "IT") == "+393331234567"
    assert parse.phone("333 123 4567", "IT") == "+393331234567"
    assert parse.phone("call me maybe", "IT") == "call me maybe"


def test_vat_prefers_country():
    assert parse.vat("IT 01234567890", "IT") == "IT01234567890"
    assert parse.vat("DE 123456789, IT 01234567890", "IT") == "IT01234567890"
    assert parse.vat("DE 123456789", "IT") == "DE123456789"
    assert parse.vat("01234567890", "IT") == "01234567890"


def test_normalize_moves_invalid_email():
    out = parse.normalize({"email": "write to us on the site", "phone": "+39 3331234567"}, "IT")
    assert "email" not in out
    assert out["phone"] == "+393331234567"
    assert "write to us" in out["further_info"]


def test_category_parsing():
    assert parse.category("https://www.ebay.it/b/Freni-e-ricambi-per-moto/49790/bn_16548780?_pgn=2", SITE) == (
        "49790",
        "https://www.ebay.it/b/Freni-e-ricambi-per-moto/49790/bn_16548780",
    )
    assert parse.category("https://www.ebay.it/b/Ricambi-per-moto/10063", SITE)[0] == "10063"
    assert parse.category("https://www.ebay.it/itm/123456789012", SITE) is None
    assert parse.category("https://www.ebay.it/n/all-categories", SITE) is None


def test_child_categories_drops_filters_and_self():
    links = [
        ("https://www.ebay.it/b/Ricambi-per-moto/10063/bn_1", "Moto: ricambi"),               # parent, kept
        ("https://www.ebay.it/b/Cavi-del-freno-per-moto/177920/bn_2", "Cavi del freno\n- Freni"),
        ("https://www.ebay.it/b/Cavi-del-freno-per-moto/177920/bn_2?x=1", "dup"),
        ("https://www.ebay.it/b/Ricambi-Acerbis-per-moto/49790", "Acerbis(146,829)"),        # filter on own id
        ("https://www.ebay.it/itm/123456789012", "an item"),
    ]
    assert parse.child_categories(links, SITE, own_id="49790") == [
        ("10063", "https://www.ebay.it/b/Ricambi-per-moto/10063/bn_1", "Moto: ricambi"),
        ("177920", "https://www.ebay.it/b/Cavi-del-freno-per-moto/177920/bn_2", "Cavi del freno"),
    ]


def test_listing_url():
    url = parse.listing_url("https://www.ebay.it/b/X/1/bn_2", "LH_PrefLoc=1&mag=1")
    assert url.format(page=3) == "https://www.ebay.it/b/X/1/bn_2?LH_PrefLoc=1&mag=1&_pgn=3"
    assert parse.listing_url("https://www.ebay.it/b/X/1/bn_2", "").format(page=1) == "https://www.ebay.it/b/X/1/bn_2?_pgn=1"


def test_root_matches():
    assert parse.root_matches("Abbigliamento e accessori", "11450", ())
    assert parse.root_matches("Abbigliamento e accessori", "11450", ("abbigliamento",))
    assert parse.root_matches("Abbigliamento e accessori", "11450", ("11450",))
    assert not parse.root_matches("Elettronica", "293", ("abbigliamento", "11450"))
