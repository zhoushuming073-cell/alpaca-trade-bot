from trading_bot.settings import config_snapshot, load_config

EXPECTED = [
    (1, "算力基建", ["CRWV", "NBIS", "IREN", "APLD", "WULF", "CIFR", "HUT", "SMCI", "DELL"]),
    (2, "存储芯片", ["MU", "SNDK", "WDC", "STX", "SKHY"]),
    (3, "AI芯片", ["NVDA", "AMD", "AVGO", "TSM", "ALAB", "MRVL"]),
    (4, "光通信", ["COHR", "LITE", "POET"]),
    (5, "商业航天", ["RKLB", "LUNR", "ASTS", "SPCX"]),
    (6, "量子计算", ["IONQ", "QBTS", "RGTI", "QUBT"]),
    (7, "AI应用", ["SOUN", "PLTR", "GOOGL", "META", "AAPL", "RDDT"]),
    (8, "数字金融", ["RIOT", "MARA", "CLSK", "COIN", "SOFI"]),
    (9, "核能科技", ["OKLO", "SMR", "NNE"]),
    (10, "创新医药", ["MRNA", "SMMT", "VKTX"]),
]


def test_watchlist_groups_partition_48_symbols():
    bundle = load_config()
    assert len(bundle.groups) == 10
    assert sum(len(group["symbols"]) for group in bundle.groups) == 48
    actual = [(group["id"], group["name"], list(group["symbols"])) for group in bundle.groups]
    assert actual == EXPECTED
    assert len(bundle.symbols) == 48
    assert set(bundle.symbols) == {symbol for _, _, symbols in EXPECTED for symbol in symbols}


def test_groups_are_exposed_in_config_snapshot():
    bundle = load_config()
    snapshot = config_snapshot(bundle)
    assert len(snapshot["groups"]) == 10
    assert snapshot["groups"][0]["name"] == "算力基建"
    assert snapshot["groups"][-1]["symbols"] == ["MRNA", "SMMT", "VKTX"]


def test_group_names_and_ids_are_stable():
    bundle = load_config()
    assert [group["id"] for group in bundle.groups] == list(range(1, 11))
    assert len({group["name"] for group in bundle.groups}) == 10
