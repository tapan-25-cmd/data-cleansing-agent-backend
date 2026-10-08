from app.services.web_check_service import FOUND, WellcomeClient, detail_queries, product_links, rank_product_links


def test_product_links_accepts_english_and_chinese_paths() -> None:
    page = '''
      <a href="/en/wellcome/p/a/i/101.html">A</a>
      <a href="/zh-hant/p/b/i/202.html">B</a>
      <a href="/en/wellcome/p/a/i/101.html">duplicate</a>
    '''
    assert product_links(page) == ["/en/wellcome/p/a/i/101.html", "/zh-hant/p/b/i/202.html"]


def test_detail_queries_are_ordered_and_deduplicated() -> None:
    assert detail_queries({
        "item_brand_eng": "ITSUKI", "item_desc_eng": "INAKA SOBA",
        "item_brand_local_lang": "五木", "item_desc_local_lang": "田舍蒿麥麵",
        "web_description_eng": "INAKA SOBA",
    }) == ["ITSUKI INAKA SOBA", "五木 田舍蒿麥麵", "INAKA SOBA", "田舍蒿麥麵"]


def test_rank_product_links_removes_default_listing_noise() -> None:
    links = ["/en/p/Other%20Product/i/1.html", "/en/p/Itsuki%20Inaka%20Soba/i/2.html"]
    assert rank_product_links(links, "ITSUKI INAKA SOBA") == ["/en/p/Itsuki%20Inaka%20Soba/i/2.html"]


def test_detail_lookup_uses_description_only_for_discovery() -> None:
    client = WellcomeClient(delay=0)
    search = '<a href="/zh-hant/p/brand-product-right/i/101.html">right</a><a href="/en/p/brand-product-wrong/i/202.html">wrong</a>'
    pages = {
        "https://www.wellcome.com.hk/en/search?keyword=BRAND%20PRODUCT": search,
        "https://www.wellcome.com.hk/zh-hant/p/brand-product-right/i/101.html": "<title>Right</title><div>Item code: 000414</div>",
        "https://www.wellcome.com.hk/en/p/brand-product-wrong/i/202.html": "<title>Wrong</title><div>Item code: 999999</div>",
    }
    client._get = pages.__getitem__  # type: ignore[method-assign]
    result = client.lookup_by_details("000414", {"item_brand_eng": "BRAND", "item_desc_eng": "PRODUCT"})
    assert result["status"] == FOUND
    assert result["item_code"] == "000414"
    assert result["matched_query"] == "BRAND PRODUCT"
