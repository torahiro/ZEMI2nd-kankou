# server.py (自然言語処理・テキスト解析強化版)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class PlanRequest(BaseModel):
    user_text: str

class ReviewRequest(BaseModel):
    reviews: list[str]

# 🔍 1. カテゴリ別感情・キーワード辞書
CATEGORY_DICTIONARY = {
    "gourmet": ["食", "海鮮", "カニ", "美味", "肉", "食べ", "グルメ", "名物", "丼", "酒", "ランチ", "ディナー"],
    "sightseeing": ["景色", "絶景", "城", "歴史", "散策", "巡り", "観光", "名所", "海", "山", "写真", "映え", "寺", "神社"],
    "healing": ["温泉", "癒やし", "ゆっくり", "のんびり", "疲れ", "静か", "リラックス", "露天風呂", "休日", "休む"]
}

NEGATION_WORDS = ["嫌", "避けたい", "くない", "ない", "ダメ", "無理", "不要", "控え"]

# -------------------------------------------------------------
# ■ 【Step-2】テキスト解析 & 潜在的嗜好ベクトル phi(t) 抽出 API
# -------------------------------------------------------------
@app.post("/analyze_preference")
async def analyze_preference(req: PlanRequest):
    text = req.user_text
    
    # スコア初期値
    scores = {"gourmet": 0.2, "sightseeing": 0.2, "healing": 0.2}
    
    if not text.strip():
        return {"status": "success", "preference_vector": scores}

    # 句読点で文を分割して文脈解析
    sentences = re.split(r'[。！!？?\n]', text)
    
    for sentence in sentences:
        if not sentence:
            continue
        
        # 否定文判定（例: 「人混みは嫌だ」「混雑は避けたい」など）
        is_negative = any(neg in sentence for neg in NEGATION_WORDS)
        
        for cat, keywords in CATEGORY_DICTIONARY.items():
            for kw in keywords:
                if kw in sentence:
                    if is_negative:
                        scores[cat] = max(0.0, scores[cat] - 0.2) # 否定要素は減点
                    else:
                        scores[cat] = min(1.0, scores[cat] + 0.3) # 肯定要素は加点

    # 正規化（0.0 〜 1.0 の範囲に収める）
    for cat in scores:
        scores[cat] = round(max(0.1, min(1.0, scores[cat])), 2)

    return {
        "status": "success",
        "preference_vector": scores,
        "analyzed_length": len(text)
    }

# -------------------------------------------------------------
# ■ 口コミ文テキストの意味・感情分析 API
# -------------------------------------------------------------
@app.post("/analyze")
async def analyze_reviews(req: ReviewRequest):
    score = 0.0
    positive_kw = ["最高", "絶品", "素晴らしい", "美味しい", "静か", "満足", "おすすめ"]
    negative_kw = ["混雑", "並ぶ", "長い", "高い", "微妙", "残念"]

    for rev in req.reviews:
        for p in positive_kw:
            if p in rev:
                score += 1.5
        for n in negative_kw:
            if n in rev:
                score -= 0.5
            
    return {
        "sentiment_score": round(score, 1),
        "summary": f"口コミ解析完了: ポジティブ感度 {score:.1f}pt"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)