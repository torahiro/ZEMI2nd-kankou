// 🌟 マスタデータ（各観光地の属性ベクトル c 用のカテゴリ値・旬シーズン）
const SAMPLE_PLACES = [
    { name: "熱海温泉", area: "東海", gourmet: 4, sightseeing: 3, healing: 5, best_season: "冬", keyword: "熱海", transit_cost: 15000, transit_time: 2.0, bus_cost: 3000, bus_time: 3.5, driving_cost: 8000, driving_time: 2.5, reviews: ["冬の花火大会が最高", "海鮮丼のコスパが良い"] },
    { name: "箱根湯本", area: "関東", gourmet: 4, sightseeing: 4, healing: 5, best_season: "冬", keyword: "箱根", transit_cost: 22000, transit_time: 1.5, bus_cost: 2500, bus_time: 2.5, driving_cost: 7000, driving_time: 2.0, reviews: ["温泉の泉質が素晴らしい", "美術館が充実"] },
    { name: "金沢・兼六園", area: "北陸", gourmet: 5, sightseeing: 5, healing: 3, best_season: "春", keyword: "金沢", transit_cost: 35000, transit_time: 3.0, bus_cost: 5000, bus_time: 7.5, driving_cost: 22000, driving_time: 6.5, reviews: ["近江町市場のカニが絶品", "兼六園の早朝入園が静か"] }
];

// 主要拠点・観光地の座標データ
const LOCATION_COORDINATES = {
    "東京駅": { lat: 35.6812, lng: 139.7671 },
    "熱海温泉": { lat: 35.0966, lng: 139.0716 },
    "箱根湯本": { lat: 35.2333, lng: 139.1036 },
    "金沢・兼六園": { lat: 36.5621, lng: 136.6622 },
    "金沢駅": { lat: 36.5780, lng: 136.6478 },
    "富山駅（白えび丼）": { lat: 36.7013, lng: 137.2133 },
    "富山駅": { lat: 36.7013, lng: 137.2133 },
    "金沢ひがし茶屋街": { lat: 36.5726, lng: 136.6666 },
    "小田原城址散策": { lat: 35.2509, lng: 139.1536 }
};

const WAYPOINT_DATABASE = [
    { targetKeyword: "金沢", name: "富山駅（白えび丼）", query: "富山駅", type: "gourmet" },
    { targetKeyword: "金沢", name: "金沢ひがし茶屋街", query: "ひがし茶屋街", type: "sightseeing" },
    { targetKeyword: "熱海", name: "小田原城址散策", query: "小田原駅", type: "sightseeing" }
];

let customWaypoints = [];
let selectedWpNames = [];
let activeDestination = null;

// DOM イベントハンドラのバインド
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('btn-gps').addEventListener('click', getGPSLocation);
    document.getElementById('budget-slider').addEventListener('input', (e) => {
        document.getElementById('budget-value').textContent = parseInt(e.target.value).toLocaleString();
    });
    document.getElementById('duration-slider').addEventListener('input', (e) => {
        document.getElementById('duration-value').textContent = e.target.value;
    });
    document.querySelectorAll('input[name="duration-type"]').forEach(radio => {
        radio.addEventListener('change', toggleDurationUI);
    });
    document.getElementById('btn-submit').addEventListener('click', handlePlanExecution);
    document.getElementById('btn-add-wp').addEventListener('click', addCustomWaypoint);
});

// 2点間の直線距離（km）をハバースインの公式で計算
function calculateDistanceKm(loc1Name, loc2Name) {
    const p1 = LOCATION_COORDINATES[loc1Name] || { lat: 35.6812, lng: 139.7671 }; 
    const p2 = LOCATION_COORDINATES[loc2Name] || { lat: 36.5621, lng: 136.6622 }; 

    const R = 6371; 
    const dLat = (p2.lat - p1.lat) * Math.PI / 180;
    const dLng = (p2.lng - p1.lng) * Math.PI / 180;
    const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
              Math.cos(p1.lat * Math.PI / 180) * Math.cos(p2.lat * Math.PI / 180) *
              Math.sin(dLng / 2) * Math.sin(dLng / 2);
    const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    return R * c;
}

// 🗺️ 現実的な交通機関・ルートに応じた移動時間・運賃推計ロジック
async function fetchGoogleMapsRouteData(origin, destination, mode) {
    const distKm = calculateDistanceKm(origin, destination);
    
    let travelMinutes = 0;
    let estimatedCost = 0;

    const routePair = `${origin}->${destination}`;

    // 個別区間の現実ダイヤ マッピング
    if (routePair.includes("東京駅") && routePair.includes("富山")) {
        travelMinutes = 135; // 北陸新幹線 約2時間15分
        estimatedCost = 12760;
    } else if (routePair.includes("富山") && routePair.includes("金沢")) {
        travelMinutes = 40;  // 新幹線 + バス/タクシー 約40分
        estimatedCost = 3100;
    } else if (routePair.includes("東京駅") && routePair.includes("金沢")) {
        travelMinutes = 150; // 北陸新幹線 約2時間30分
        estimatedCost = 14380;
    } else if (routePair.includes("東京駅") && routePair.includes("熱海")) {
        travelMinutes = 45;  // 東海道新幹線 約45分
        estimatedCost = 4280;
    } else {
        let speedKmH = 60;
        let costPerKm = 20;
        let fixedCost = 0;
        let baseDelayMin = 15;

        switch (mode) {
            case 'transit':
                if (distKm > 100) {
                    speedKmH = 120;
                    fixedCost = 3500;
                    costPerKm = 22;
                    baseDelayMin = 20;
                } else {
                    speedKmH = 45;
                    costPerKm = 18;
                    baseDelayMin = 15;
                }
                break;
            case 'driving':
                speedKmH = distKm > 50 ? 70 : 30;
                costPerKm = 18;
                fixedCost = distKm > 50 ? 1500 : 0;
                baseDelayMin = 10;
                break;
            case 'bus':
                speedKmH = distKm > 50 ? 55 : 25;
                costPerKm = 10;
                baseDelayMin = 20;
                break;
        }

        travelMinutes = Math.max(15, Math.round((distKm / speedKmH) * 60 + baseDelayMin));
        estimatedCost = Math.max(300, Math.round(distKm * costPerKm + fixedCost));
    }

    const worstMinutes = Math.round(travelMinutes * 1.4 + 20);
    const worstCost = Math.round(estimatedCost * 1.3);

    return new Promise((resolve) => {
        setTimeout(() => {
            resolve({
                duration_minutes: travelMinutes,
                estimated_cost: estimatedCost,
                distance_km: Math.round(distKm),
                worst_time: worstMinutes,
                worst_cost: worstCost
            });
        }, 50);
    });
}

// ■【Step-2】Pythonバックエンド API 呼び出し
async function fetchPreferenceVectorAPI(userText) {
    try {
        const response = await fetch('http://localhost:8000/analyze_preference', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_text: userText })
        });
        if (response.ok) {
            return await response.json();
        }
    } catch (e) {
        console.warn("Pythonサーバー未接続のため、デフォルトベクトルを使用します");
    }
    return {
        preference_vector: { gourmet: 0.5, sightseeing: 0.5, healing: 0.5 }
    };
}

// ■ 10項目のこだわり度（多目的重み付け）取得関数
function getUserPreferences() {
    const prefs = {};
    document.querySelectorAll('.pref-select').forEach(select => {
        const key = select.getAttribute('data-key');
        prefs[key] = parseFloat(select.value);
    });
    return prefs;
}

async function analyzeReviewsAPI(reviewsArray) {
    try {
        return {
            sentiment_score: reviewsArray.length * 1.5,
            summary: `🗣️ **リアル口コミ解析結果**: ${reviewsArray.join(' / ')}`
        };
    } catch (e) {
        return { sentiment_score: 0, summary: "口コミ解析失敗" };
    }
}

function getGPSLocation() {
    const input = document.getElementById('start-location');
    if (!navigator.geolocation) return alert("GPS非対応です");
    input.value = "位置情報取得中...";
    navigator.geolocation.getCurrentPosition(
        (pos) => input.value = `${pos.coords.latitude},${pos.coords.longitude}`,
        () => input.value = "東京駅"
    );
}

function toggleDurationUI() {
    const isHours = document.querySelector('input[name="duration-type"]:checked').value === 'hours';
    const slider = document.getElementById('duration-slider');
    document.getElementById('duration-unit').textContent = isHours ? "時間" : "日間";
    slider.min = 1; slider.max = isHours ? 12 : 7; slider.value = isHours ? 8 : 2;
    document.getElementById('duration-value').textContent = slider.value;
}

// ■【Step-1】メイン処理エントリーポイント
async function handlePlanExecution() {
    const endLoc = document.getElementById('end-location').value.trim();
    const userText = document.getElementById('user-text-intent').value.trim();

    customWaypoints = []; selectedWpNames = [];
    document.getElementById('customWaypointsTags').innerHTML = '';

    if (!endLoc) {
        await runDiagnosisProcess(userText);
        document.getElementById('custom-section').style.display = 'none';
    } else {
        document.getElementById('diagnosis-section').style.display = 'none';
        let matchedKw = "直行";
        SAMPLE_PLACES.forEach(p => { if (endLoc.includes(p.keyword)) matchedKw = p.keyword; });
        activeDestination = { name: endLoc, keyword: matchedKw };
        renderCustomSection();
    }
}

// ■【Step-2 & Step-3】嗜好ベクトル統合 & 多目的スコアリング判定
async function runDiagnosisProcess(userText) {
    const b = getUserPreferences();
    const prefResult = await fetchPreferenceVectorAPI(userText);
    const phi = prefResult.preference_vector;

    // 【式(1)】u = alpha * phi(t) + (1 - alpha) * psi(b)
    const alpha = 0.5;

    const b_gourmet = Math.max(0, b.gourmet);
    const b_sightseeing = Math.max(0, b.sightseeing) + Math.max(0, b.event) * 0.5 + Math.max(0, b.pilgrimage) * 0.5;
    const b_healing = Math.max(0, b.relax_schedule) + Math.max(0, b.safety) * 0.5;

    const u = {
        gourmet: alpha * phi.gourmet + (1 - alpha) * b_gourmet,
        sightseeing: alpha * phi.sightseeing + (1 - alpha) * b_sightseeing,
        healing: alpha * phi.healing + (1 - alpha) * b_healing
    };

    const debugArea = document.getElementById('analysis-debug-area');
    const vectorOutput = document.getElementById('vector-output');
    if (debugArea && vectorOutput) {
        debugArea.style.display = 'block';
        vectorOutput.innerHTML = `
            <strong>統合嗜好ベクトル u (式(1)):</strong><br>
            グルメ: <code>${u.gourmet.toFixed(2)}</code> | 観光: <code>${u.sightseeing.toFixed(2)}</code> | 癒やし/安全: <code>${u.healing.toFixed(2)}</code><br>
            <small style="color:#666;">※10項目のこだわり評価（混雑回避:${b.avoid_crowd}, イベント:${b.event}, 旬:${b.seasonal} 等）を計算反映中</small>
        `;
    }

    const budgetLimit = parseInt(document.getElementById('budget-slider').value);
    const transMode = document.getElementById('transport-mode').value;
    const allowedModes = (transMode === 'any') ? ['transit', 'bus', 'driving'] : [transMode];

    let results = [];
    for (let place of SAMPLE_PLACES) {
        let bestMode = null; 
        let lowestCost = Infinity;

        allowedModes.forEach(m => {
            let cost = place[`${m}_cost`];
            if (cost <= budgetLimit && cost < lowestCost) { 
                lowestCost = cost; 
                bestMode = m; 
            }
        });

        if (bestMode) {
            // 【式(2)】コサイン類似度
            const c = { 
                gourmet: place.gourmet / 5.0, 
                sightseeing: place.sightseeing / 5.0, 
                healing: place.healing / 5.0 
            };
            
            const dotProduct = (u.gourmet * c.gourmet) + (u.sightseeing * c.sightseeing) + (u.healing * c.healing);
            const normU = Math.sqrt(u.gourmet**2 + u.sightseeing**2 + u.healing**2) || 1;
            const normC = Math.sqrt(c.gourmet**2 + c.sightseeing**2 + c.healing**2) || 1;
            const cosSim = dotProduct / (normU * normC);

            let baseScore = cosSim * 40;

            // 10項目のこだわり設定補正
            let preferenceBonus = 0;

            if (b.avoid_crowd === 1) {
                if (place.name.includes("箱根") || place.name.includes("兼六園")) preferenceBonus -= 8;
                else preferenceBonus += 5;
            }

            if (b.discount === 1 && lowestCost <= budgetLimit * 0.7) {
                preferenceBonus += 5;
            }

            const currentSeason = document.getElementById('travel-month').value;
            if (b.seasonal === 1 && place.best_season === currentSeason) {
                preferenceBonus += 10;
            } else if (b.seasonal === -1 && place.best_season === currentSeason) {
                preferenceBonus -= 10;
            }

            if (b.safety === 1) {
                preferenceBonus += (place.healing >= 4) ? 5 : -5;
            }

            if (b.pilgrimage === 1 && (place.keyword === "金沢" || place.keyword === "箱根")) {
                preferenceBonus += 8;
            }

            if (b.event === 1 && place.name.includes("熱海")) {
                preferenceBonus += 7;
            }

            if (b.packed_schedule === 1 && place.transit_time <= 2.0) {
                preferenceBonus += 5;
            }

            if (b.relax_schedule === 1 && place.healing >= 4) {
                preferenceBonus += 8;
            }

            const reviewAnalysis = await analyzeReviewsAPI(place.reviews);
            const finalScore = Math.max(0, Math.min(100, 50 + baseScore + preferenceBonus + reviewAnalysis.sentiment_score));

            results.push({ 
                ...place, 
                score: finalScore, 
                chosen_mode: bestMode, 
                cost: lowestCost, 
                review_summary: reviewAnalysis.summary 
            });
        }
    }

    results.sort((a, b) => b.score - a.score);
    const grid = document.getElementById('top3Grid'); 
    grid.innerHTML = '';

    if (results.length === 0) {
        grid.innerHTML = '<p>条件に適合する旅行先がありませんでした。</p>';
    } else {
        results.slice(0, 3).forEach((res, idx) => {
            const card = document.createElement('div'); 
            card.className = 'rank-card';
            card.innerHTML = `
                <div>
                    <strong>第${idx+1}位: ${res.name}</strong>
                    <div class="score-display">適合度スコア: ${res.score.toFixed(1)}点</div>
                    <div class="review-summary">${res.review_summary}</div>
                    <p style="font-size:0.8rem;">移動: ${res.chosen_mode} / 費用: ${res.cost.toLocaleString()}円</p>
                </div>
                <button class="btn-select-place" onclick="selectDiagnosedPlace('${res.name}', '${res.keyword}')">この目的地で決定 ➔</button>
            `;
            grid.appendChild(card);
        });
    }
    document.getElementById('diagnosis-section').style.display = 'block';
}

function selectDiagnosedPlace(name, keyword) {
    activeDestination = { name, keyword };
    renderCustomSection();
}

function renderCustomSection() {
    document.getElementById('route-subtitle').textContent = `目的地: ${activeDestination.name} の最適移動経路`;
    const wpArea = document.getElementById('recommended-waypoints-area'); wpArea.innerHTML = '';
    
    const matchedWps = WAYPOINT_DATABASE.filter(wp => wp.targetKeyword === activeDestination.keyword);
    matchedWps.forEach((wp, idx) => {
        const item = document.createElement('div'); item.className = 'wp-item';
        item.innerHTML = `<input type="checkbox" id="wp-${idx}" data-idx="${idx}" checked> 📍 ${wp.name} [${wp.type}]`;
        wpArea.appendChild(item);
    });

    document.querySelectorAll('#recommended-waypoints-area input').forEach(cb => {
        cb.addEventListener('change', handleWpChange);
    });

    document.getElementById('custom-section').style.display = 'block';
    handleWpChange();
}

async function handleWpChange() {
    selectedWpNames = [];
    const matchedWps = WAYPOINT_DATABASE.filter(wp => wp.targetKeyword === activeDestination.keyword);
    document.querySelectorAll('#recommended-waypoints-area input:checked').forEach(cb => {
        const idx = cb.getAttribute('data-idx');
        selectedWpNames.push(matchedWps[idx]);
    });
    await generateTimelineWithBenchmark();
}

async function addCustomWaypoint() {
    const input = document.getElementById('new-wp-input');
    if (input.value.trim()) {
        customWaypoints.push({ 
            name: input.value, 
            query: input.value, 
            type: 'sightseeing'
        });
        input.value = '';
        renderCustomTags();
        await generateTimelineWithBenchmark();
    }
}

function renderCustomTags() {
    const tags = document.getElementById('customWaypointsTags'); tags.innerHTML = '';
    customWaypoints.forEach((wp) => {
        const tag = document.createElement('span'); tag.className = 'custom-tag';
        tag.textContent = wp.name;
        tags.appendChild(tag);
    });
}

// ⚡ ベンチマーク時間計測
async function generateTimelineWithBenchmark() {
    const startTime = performance.now();
    await generateTimeline();
    const endTime = performance.now();

    const perfArea = document.getElementById('performance-area');
    const benchCard = document.createElement('div'); benchCard.className = 'perf-card bench-style';
    benchCard.innerHTML = `<h5>⚡ AI処理速度</h5><div class="perf-num">${(endTime - startTime).toFixed(2)} ms</div><div class="perf-desc">多目的制約充足の計算完了</div>`;
    perfArea.appendChild(benchCard);
}

// ■【Step-4〜Step-6】体験ピーク構造に基づく滞在時間非均一自動配分 & 旅程出力
async function generateTimeline() {
    const startLoc = document.getElementById('start-location').value;
    const rawMode = document.getElementById('transport-mode').value;
    const transMode = rawMode === 'any' ? 'transit' : rawMode;
    
    const peakSetting = document.querySelector('input[name="plan-peak"]:checked')?.value || 'early';

    const timelineArea = document.getElementById('timeline-area');
    const perfArea = document.getElementById('performance-area');
    
    timelineArea.innerHTML = ''; 
    perfArea.innerHTML = '';

    let baseWaypoints = [...selectedWpNames, ...customWaypoints];

    let savedTime = 0; 
    let savedCost = 0;
    let currentLoc = startLoc;

    const dayContainer = document.createElement('div'); 
    dayContainer.className = 'day-section';
    
    let currentHour = 9; // 9:00 出発
    let currentMin = 0;

    const totalNodes = baseWaypoints.length;

    for (let i = 0; i < totalNodes; i++) {
        const wp = baseWaypoints[i];

        const routeData = await fetchGoogleMapsRouteData(currentLoc, wp.name, transMode);
        
        const actualTime = routeData.duration_minutes; 
        const actualCost = routeData.estimated_cost;

        savedTime += (routeData.worst_time - actualTime);
        savedCost += (routeData.worst_cost - actualCost);

        const timeDisplay = actualTime >= 60 
            ? `約 ${Math.floor(actualTime / 60)}時間${actualTime % 60}分` 
            : `約 ${actualTime} 分`;

        const arrow = document.createElement('div'); 
        arrow.className = 'tl-arrow';
        arrow.textContent = `↓ 移動 (${currentLoc} ➔ ${wp.name}): ${timeDisplay} [約${routeData.distance_km}km] (想定運賃: ${actualCost.toLocaleString()}円)`;
        dayContainer.appendChild(arrow);

        currentMin += actualTime;
        if (currentMin >= 60) { 
            currentHour += Math.floor(currentMin / 60); 
            currentMin %= 60; 
        }

        // 【Step-4】ピーク構造に基づく滞在時間（120分 vs 60分） (式(8))
        let stayMinutes = 60;
        let isPeak = false;

        if (peakSetting === 'early' && i === 0) isPeak = true;
        else if (peakSetting === 'middle' && i === Math.floor(totalNodes / 2)) isPeak = true;
        else if (peakSetting === 'late' && i === totalNodes - 1) isPeak = true;

        if (isPeak) {
            stayMinutes = 120;
        }

        const timeStr = `${String(currentHour).padStart(2, '0')}:${String(currentMin).padStart(2, '0')}`;
        const item = document.createElement('div'); 
        item.className = 'timeline-item';
        item.innerHTML = `
            <div class="tl-time">${timeStr}</div>
            <div class="tl-badge" style="background:${isPeak ? '#e67e22' : '#007aff'};"></div>
            <div class="tl-card ${isPeak ? 'warning' : 'info'}">
                📍 <strong>${wp.name}</strong> ${isPeak ? '<span style="color:#e67e22; font-weight:bold;">🔥 [体験ピーク地点]</span>' : ''}
                <span style="font-size:0.8rem; color:#666; display:block;">(滞在時間: ${stayMinutes}分)</span>
            </div>`;
        dayContainer.appendChild(item);

        currentMin += stayMinutes;
        if (currentMin >= 60) { 
            currentHour += Math.floor(currentMin / 60); 
            currentMin %= 60; 
        }

        currentLoc = wp.name;
    }

    const costCard = document.createElement('div'); 
    costCard.className = 'perf-card cost-style';
    costCard.innerHTML = `<h5>💰 コスパ効果 (最適ルート削減額)</h5><div class="perf-num">-${savedCost.toLocaleString()}円</div>`;
    perfArea.appendChild(costCard);

    const timeCard = document.createElement('div'); 
    timeCard.className = 'perf-card time-style';
    timeCard.innerHTML = `<h5>⏱️ タイパ効果 (時間短縮)</h5><div class="perf-num">${savedTime} 分短縮</div>`;
    perfArea.appendChild(timeCard);

    // 【Step-6】Google Maps 連携リンク生成
    const CHUNK_SIZE = 8;
    let queries = baseWaypoints.map(w => w.query || w.name);
    const destName = activeDestination ? activeDestination.name : '';
    for (let i = 0; i < Math.max(1, queries.length); i += CHUNK_SIZE) {
        let chunk = queries.slice(i, i + CHUNK_SIZE);
        let url = `https://www.google.com/maps/dir/?api=1&origin=${encodeURIComponent(startLoc)}&destination=${encodeURIComponent(destName)}&waypoints=${chunk.map(encodeURIComponent).join('|')}`;
        const mapBtn = document.createElement('a'); 
        mapBtn.className = 'btn-day-map'; 
        mapBtn.href = url; 
        mapBtn.target = '_blank';
        mapBtn.textContent = `🗺️ Google Mapで実際の経路を確認する (Part ${Math.floor(i/CHUNK_SIZE) + 1})`;
        dayContainer.appendChild(mapBtn);
    }

    timelineArea.appendChild(dayContainer);
}