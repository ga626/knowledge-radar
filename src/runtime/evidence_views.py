"""可逆减负建议：只比较当前阅读集合，不删除、不推断来源独立性。"""
import re
import unicodedata


def normalized(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC",text)).strip().casefold()


def guards(text):
    return (tuple(re.findall(r"\d+(?:[.,]\d+)*",text)),
            tuple(re.findall(r"不|未|无|非|没有|禁止|not|never|no|without",text.casefold())))


def suggestions(items):
    rows, result = [], []
    for item in items[:100]:
        text = normalized(str(item.get("snippet") or ""))
        if len(text)<40:
            continue
        shingles = {text[i:i+5] for i in range(len(text)-4)}
        for old,other,parts in rows:
            exact = text == other
            overlap = len(shingles & parts)/max(1,len(shingles | parts))
            if exact or overlap>=0.82:
                protected = guards(text)!=guards(other) or item.get("parent_id")!=old.get("parent_id")
                result.append({"candidate_id":item["candidate_id"],"related_candidate_id":old["candidate_id"],
                    "reason":"exact_snippet" if exact else "near_snippet", "similarity":round(overlap,4),
                    "protected_difference":protected,"suggest_fold":not protected,
                    "independence":"unknown","automatic_action":"none","algorithm":"char5-jaccard/v1"})
        rows.append((item,text,shingles))
    return result
