// app.js
let currentCandidates = [];
let customWaypointsList = [];
let lastGeneratedItineraryData = null;
let selectedRouteIdx = null;
let lastDetailedVector = {}; // 診断時の「10項目こだわり」入力を旅程生成側でも使うため保持（滞在ペース等に反映）
let mainPeakPlace = null;    // 選択したメインピーク（式7・8のτ^peak/ピーク重みを適用する唯一の対象）
// TOP3のうち選ばなかった残りを「AIが自動提案する経由地」として初期セットしたもの。
// 以前は経由地は完全に手動入力のみだったが、しおりは最初から経由地込みで自動提案し、
// そこから自分で削除（不要なら✕で外す）できるようにする。
let autoWaypoints = [];
 
// ローカル開発時（intoro.htmlをLive Server等で127.0.0.1:5500のような別ポートから開き、
// バックエンドをlocalhost:8000で別プロセス起動している場合）はlocalhost:8000を直接叩く。
// デプロイ後（Render等でintoro.html/app.js自体もこのFastAPIサービスから配信される場合）は
// フロントエンドとAPIが同じオリジンになるため、相対パス（空文字）でそのまま同じホストを叩けばよい。
// これによりデプロイ先のURLをapp.js内にハードコードする必要がなくなる。
const API_BASE_URL = (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
    ? 'http://localhost:8000'
    : '';
 
document.addEventListener('DOMContentLoaded', () => {
    // スライダー表示連動
    const durSlider = document.getElementById('duration-slider');
    if (durSlider) {
        durSlider.addEventListener('input', (e) => {
            const val = parseInt(e.target.value, 10);
            document.getElementById('duration-value').textContent = val;
            document.getElementById('duration-hours').textContent = (val / 60).toFixed(1);
        });
    }
 
    const budgetSlider = document.getElementById('budget-slider');
    if (budgetSlider) {
        budgetSlider.addEventListener('input', (e) => {
            document.getElementById('budget-value').textContent = parseInt(e.target.value, 10).toLocaleString();
        });
    }
 
    // 旅行日数連動
    const tripDaysSelect = document.getElementById('trip-days');
    if (tripDaysSelect) {
        tripDaysSelect.addEventListener('change', () => {
            const isMultiDay = parseInt(tripDaysSelect.value, 10) > 0;
            const multiNote = document.getElementById('multiday-note');
            if (multiNote) multiNote.style.display = isMultiDay ? 'block' : 'none';
        });
    }
 
    // ボタンイベント登録
    document.getElementById('btn-diagnose')?.addEventListener('click', runDiagnosis);
    document.getElementById('btn-retry-diagnose')?.addEventListener('click', runDiagnosis);
    document.getElementById('btn-build')?.addEventListener('click', generateFinalItinerary);
    document.getElementById('btn-favorite')?.addEventListener('click', saveToFavorites);
    document.getElementById('btn-add-waypoint')?.addEventListener('click', addCustomWaypoint);
 
    // 経由地入力 Enter キー
    document.getElementById('waypoint-input')?.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            addCustomWaypoint();
        }
    });
 
    // 動的タグ削除のイベント委譲
    document.getElementById('waypoints-tag-container')?.addEventListener('click', (e) => {
        const btn = e.target.closest('.btn-remove-wp');
        if (btn) removeCustomWaypoint(parseInt(btn.dataset.index, 10));
    });
 
    // AI自動提案の経由地（TOP3のうち選ばなかった残り）の削除ボタン
    document.getElementById('auto-waypoints-list')?.addEventListener('click', (e) => {
        const btn = e.target.closest('.btn-remove-auto-wp');
        if (btn) removeAutoWaypoint(parseInt(btn.dataset.index, 10));
    });
 
    // TOP3カード選択のイベント委譲
    document.getElementById('top3-grid')?.addEventListener('click', (e) => {
        const card = e.target.closest('.rank-card');
        if (card) {
            const idx = parseInt(card.dataset.idx, 10);
            selectRoute(idx);
        }
    });
 
    // 星評価UI
    initStarRating();
 
    loadFavorites();
});
 
/* --- 経由地タグの操作 --- */
function addCustomWaypoint() {
    const input = document.getElementById('waypoint-input');
    if (!input) return;
    const val = input.value.trim();
    if (val && !customWaypointsList.includes(val)) {
        customWaypointsList.push(val);
        input.value = '';
        renderWaypointTags();
    }
}
 
function removeCustomWaypoint(index) {
    if (index >= 0 && index < customWaypointsList.length) {
        customWaypointsList.splice(index, 1);
        renderWaypointTags();
    }
}
 
function renderWaypointTags() {
    const container = document.getElementById('waypoints-tag-container');
    if (!container) return;
    container.innerHTML = customWaypointsList.map((wp, idx) => `
        <span class="waypoint-tag custom">
            💡 ${escapeHtml(wp)}
            <button type="button" class="btn-remove-wp" data-index="${idx}">✕</button>
        </span>
    `).join('');
}
 
/* --- AI自動提案の経由地（TOP3のうち選ばなかった残り）の操作 --- */
function removeAutoWaypoint(index) {
    if (index >= 0 && index < autoWaypoints.length) {
        autoWaypoints.splice(index, 1);
        renderAutoWaypoints();
    }
}
 
function renderAutoWaypoints() {
    const container = document.getElementById('auto-waypoints-list');
    if (!container) return;
    if (autoWaypoints.length === 0) {
        container.innerHTML = '<span style="font-size:0.85rem; color:#94a3b8;">（自動提案できる残りの候補地はありません）</span>';
        return;
    }
    container.innerHTML = autoWaypoints.map((wp, idx) => `
        <span class="waypoint-tag auto">
            🤖 ${escapeHtml(wp.name)}
            <button type="button" class="btn-remove-auto-wp" data-index="${idx}" title="この経由地を旅程から外す">✕</button>
        </span>
    `).join('');
}
 
function renderDroppedNotice(names) {
    const el = document.getElementById('dropped-notice');
    if (!el) return;
    if (!names || names.length === 0) {
        el.style.display = 'none';
        el.innerHTML = '';
        return;
    }
    el.style.display = 'block';
    el.innerHTML = `⚠️ 予算・時間・営業時間の制約に収めるため、以下の経由地は今回の旅程から自動的に除外されました：${names.map(escapeHtml).join('、')}`;
}
 
// 出発地点・対象エリアは、以前「東京駅」「箱根温泉」を初期値としてフォームに入れていたが、
// 入力し忘れてもそのまま無言でその場所として診断・旅程生成されてしまい、ユーザーの意図と
// 無関係な（自分では選んでいない）場所が使われる原因になっていた。フォーム側の初期値は
// 空にした上で、ここで未入力を検出してエラーにする（無意味な既定値に静かにフォールバック
// させない）。
function validateRequiredLocationFields() {
    const startLoc = document.getElementById('start-location')?.value?.trim();
    if (!startLoc) {
        alert('出発地点を入力してください。');
        document.getElementById('start-location')?.focus();
        return false;
    }
    const area = document.getElementById('target-area')?.value?.trim();
    if (!area) {
        alert('対象エリア（旅行先）を入力してください。');
        document.getElementById('target-area')?.focus();
        return false;
    }
    return true;
}
 
/* --- 5. 診断実行 (TOP3表示) --- */
async function runDiagnosis() {
    if (!validateRequiredLocationFields()) return;
 
    const btnDiagnose = document.getElementById('btn-diagnose');
    const loading = document.getElementById('diagnose-loading');
    const failBanner = document.getElementById('ai-fail-banner');
 
    if (btnDiagnose) btnDiagnose.disabled = true;
    if (loading) loading.style.display = 'flex';
    if (failBanner) failBanner.style.display = 'none';
 
    const detailedVector = {};
    document.querySelectorAll('.detailed-vec').forEach(select => {
        const key = select.getAttribute('data-key');
        if (key) detailedVector[key] = parseFloat(select.value);
    });
    lastDetailedVector = detailedVector; // 旅程生成(build_itinerary)側の滞在ペース調整で再利用する
 
    const payload = {
        user_text: document.getElementById('user-text-intent')?.value || '',
        // preferences_4step は廃止（旧固定値のダミー送信をやめ、10項目こだわり入力(detailed_vector)から
        // サーバー側で式(1)のψ(b)を構築するようにした）
        detailed_vector: detailedVector,
        target_area: document.getElementById('target-area')?.value?.trim() || '',
        season: document.getElementById('season-select')?.value || 'winter',
        generation_group: document.getElementById('generation-group')?.value || 'couple',
        custom_reviews_text: document.getElementById('custom-reviews-text')?.value || '',
        // 出発時間・出発地点・到着地点・費用・移動手段・行程タイプの制約。以前は旅程生成の
        // 段階でしか使っていなかったため、選んだ候補が実は予算・時間・営業時間に収まらない、
        // ということが旅程生成まで分からなかった。診断（TOP3提案）の時点から渡すことで、
        // 制約内に収まる候補を優先して提案してもらう。
        start_location: document.getElementById('start-location')?.value?.trim() || '',
        end_location: document.getElementById('end-location-place')?.value?.trim() || '',
        start_time: document.getElementById('start-time')?.value || '09:00',
        transport_mode: document.getElementById('transport-mode')?.value || 'transit',
        trip_type: document.getElementById('trip-type')?.value || 'round_trip',
        budget_limit: parseFloat(document.getElementById('budget-slider')?.value || 50000),
        time_limit: parseFloat(document.getElementById('duration-slider')?.value || 480),
        member_count: parseInt(document.getElementById('member-count')?.value || 1, 10)
    };
 
    try {
        const res = await fetch(`${API_BASE_URL}/diagnose_top3`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
 
        if (!res.ok) {
            // バックエンドがHTTPException(400等)で理由付きエラーを返してきた場合は、
            // 「エラーが発生しました」という中身のないメッセージで終わらせず、
            // その理由（例：対象エリア未入力）をそのまま利用者に伝える。
            let detail = "API通信エラー";
            try {
                const errBody = await res.json();
                if (errBody?.detail) detail = errBody.detail;
            } catch (_) { /* JSONでなければ既定メッセージのまま */ }
            throw new Error(detail);
        }
 
        const data = await res.json();
        currentCandidates = data.top3_places || [];
        renderTop3(currentCandidates, payload.target_area);
    } catch (err) {
        console.error(err);
        alert(err?.message || "目的地の提案取得中にエラーが発生しました。");
        if (failBanner) failBanner.style.display = 'block';
    } finally {
        if (btnDiagnose) btnDiagnose.disabled = false;
        if (loading) loading.style.display = 'none';
    }
}
 
function renderTop3(places, areaName) {
    const top3Section = document.getElementById('top3-section');
    if (top3Section) {
        top3Section.style.display = 'block';
        top3Section.scrollIntoView({ behavior: 'smooth' });
    }
 
    const grid = document.getElementById('top3-grid');
    if (!grid) return;
    grid.innerHTML = '';
 
    // テーマ別に3コースカードを生成。以前は3枚とも同じ候補リストを丸ごと表示していたため
    // 見た目上「どのカードも同じ目的地しか出ない」状態だった。診断スコア1〜3位の候補を
    // それぞれ別々のカードに1件ずつ割り当て、3枚で異なる目的地になるようにする。
    const themes = ["王道定番コース", "グルメ・癒し満喫コース", "穴場・のんびりコース"];
 
    themes.forEach((themeTitle, idx) => {
        const place = places[idx];
        if (!place) return; // 候補がテーマ数（3件）に満たない場合、そのカードは生成しない
 
        const card = document.createElement('div');
        card.className = 'rank-card';
        card.dataset.idx = idx;
 
        const score = place.score ?? Math.max(70, 95 - idx * 8);
        const cost = place.cost || 0;
 
        const spotItemHtml = `
            <div>
                <span class="badge ${place.t_base >= 90 ? 'badge-peak' : 'badge-circuit'}">${place.t_base >= 90 ? 'メイン(120分)' : '周遊(60分)'}</span>
                ${escapeHtml(place.name)}
                <span class="badge badge-hours">09:00〜17:00</span>
                <span class="badge badge-ai">AI提案</span>
            </div>
        `;
 
        // fits_constraints等は、出発時間・出発地点・到着地点・費用・移動手段・行程タイプの
        // 制約を診断時に渡した場合のみ付与される（未指定なら undefined のまま＝表示しない）。
        let constraintHtml = '';
        if (place.fits_constraints === true) {
            constraintHtml = `
                <div class="rank-meta">
                    <span class="badge badge-ok">✔ 指定した制約内で成立</span>
                    見積り費用 ¥${(place.estimated_total_cost || 0).toLocaleString()} ／ 所要 約${place.estimated_total_time_minutes || 0}分
                </div>`;
        } else if (place.fits_constraints === false) {
            const reasons = (place.violation_reasons || []).join('・');
            constraintHtml = `
                <div class="rank-meta">
                    <span class="badge badge-warn">⚠️ ${escapeHtml(reasons || '制約を満たせない可能性')}</span>
                    見積り費用 ¥${(place.estimated_total_cost || 0).toLocaleString()} ／ 所要 約${place.estimated_total_time_minutes || 0}分
                </div>`;
        }
 
        card.innerHTML = `
            <div>
                <div class="rank-title">${idx === 0 ? "🥇" : idx === 1 ? "🥈" : "🥉"} ${themeTitle}</div>
                <div class="score-display">適合スコア ${score} / 100</div>
                <div class="rank-spotlist">${spotItemHtml}</div>
                <div class="rank-meta">想定入場料: ¥${cost.toLocaleString()}</div>
                ${constraintHtml}
            </div>
            <button type="button" class="rank-pick-btn">このコースを選択</button>
        `;
        grid.appendChild(card);
    });
}
 
function selectRoute(idx) {
    selectedRouteIdx = idx;
    document.querySelectorAll('.rank-card').forEach((c, i) => {
        c.classList.toggle('selected', i === idx);
    });
 
    const customSection = document.getElementById('custom-section');
    if (customSection) {
        customSection.style.display = 'block';
        customSection.scrollIntoView({ behavior: 'smooth' });
    }
 
    const themes = ["王道定番コース", "グルメ・癒し満喫コース", "穴場・のんびりコース"];
    const area = document.getElementById('target-area')?.value?.trim() || '';
    const startLoc = document.getElementById('start-location')?.value?.trim() || '';
    const startTime = document.getElementById('start-time')?.value || '09:00';
    const chosenPlace = currentCandidates[idx];
 
    // 選んだカードをメインピークとし、TOP3のうち選ばなかった残りをAI自動提案の経由地として
    // 初期セットする。以前は経由地は完全に手動入力のみだったが、ここから自分で✕で削除できる。
    mainPeakPlace = chosenPlace || null;
    autoWaypoints = currentCandidates.filter((p, i) => i !== idx && p);
    renderAutoWaypoints();
 
    const subTitle = document.getElementById('route-subtitle');
    if (subTitle) {
        const spotLabel = chosenPlace ? `：${chosenPlace.name}` : '';
        subTitle.textContent = `選択コース：${themes[idx]}${spotLabel}（${area}）／出発 ${startLoc} ${startTime}`;
    }
}
 
/* --- 6. 旅程しおり生成 --- */
async function generateFinalItinerary() {
    if (!validateRequiredLocationFields()) return;
 
    const btnBuild = document.getElementById('btn-build');
    if (btnBuild) {
        btnBuild.disabled = true;
        btnBuild.textContent = "AIが実在スポット・移動時間を計算中…";
    }
 
    const peakInput = document.querySelector('input[name="plan-peak"]:checked');
    const peakValue = peakInput ? peakInput.value : "前半";
 
    // メインピーク（選択したカード）＋ AI自動提案の経由地（✕で外していない残り）を、
    // まとめて旅程の対象にする。以前は選択したカード1件のみで、経由地は手動入力しないと
    // 一切含まれなかったが、しおりは最初から経由地込みで自動提案されるようにする。
    const mainPeak = mainPeakPlace ?? currentCandidates[selectedRouteIdx] ?? currentCandidates[0];
    const selectedPlaces = [mainPeak, ...autoWaypoints].filter(Boolean);
 
    const payload = {
        selected_place_ids: selectedPlaces.map(p => p.id),
        candidate_places: selectedPlaces,
        // どれが「本当のメインピーク」（式7・8のτ^peak・ピーク重み適用対象）かをバックエンドに
        // 明示する。selected_placesにはAI自動提案の経由地も混ざっているため必須。
        main_peak_place_id: mainPeak?.id || "",
        custom_waypoints: customWaypointsList,
        start_location: document.getElementById('start-location')?.value?.trim() || '',
        // 出発地と異なる到着地点（任意）。未入力なら空文字のままバックエンド側で
        // 従来通り trip_type（往復／片道）に基づいて終着点を決める。
        end_location: document.getElementById('end-location-place')?.value?.trim() || "",
        start_time: document.getElementById('start-time')?.value || "09:00",
        end_time: document.getElementById('end-time')?.value || "",
        trip_type: document.getElementById('trip-type')?.value || "round_trip",
        transport_mode: document.getElementById('transport-mode')?.value || "transit",
        generation_group: document.getElementById('generation-group')?.value || "couple",
        member_count: parseInt(document.getElementById('member-count')?.value || 1, 10),
        peak_position: peakValue,
        budget_limit: parseFloat(document.getElementById('budget-slider')?.value || 50000),
        time_limit: parseFloat(document.getElementById('duration-slider')?.value || 480),
        // packed_schedule/relax_schedule（過密⇔ゆっくり）を旅程側の滞在時間ペースにも反映するため送る
        detailed_vector: lastDetailedVector,
        // 経由地（自由入力テキスト）のジオコーディング精度向上のためのヒント（診断時のエリア指定と同じ）。
        // 例:「ミラノ亭」のような曖昧な店名が全国の同名店と混同されるのを防ぐために使う。
        target_area: document.getElementById('target-area')?.value?.trim() || ''
    };
 
    try {
        const res = await fetch(`${API_BASE_URL}/build_itinerary`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
 
        if (!res.ok) {
            let detail = "しおり生成API通信エラー";
            try {
                const errBody = await res.json();
                if (errBody?.detail) detail = errBody.detail;
            } catch (_) { /* JSONでなければ既定メッセージのまま */ }
            throw new Error(detail);
        }
 
        const data = await res.json();
        lastGeneratedItineraryData = data;
 
        renderItinerary(data, payload.start_location);
        // 予算・時間・営業時間に収めるため自動的に外された経由地があれば通知する
        renderDroppedNotice(data.dropped_names);
 
        // 各セクションの表示解放（HTML側に該当セクションが無い場合でも
        // 「Cannot read properties of null」で処理全体が落ちないようnullガードする）
        const outputSection = document.getElementById('output-section');
        const replanSection = document.getElementById('replan-section');
        const reviewSection = document.getElementById('review-section');
        if (outputSection) outputSection.style.display = 'block';
        if (replanSection) replanSection.style.display = 'block';
        if (reviewSection) reviewSection.style.display = 'block';
 
        if (outputSection) outputSection.scrollIntoView({ behavior: 'smooth' });
 
    } catch (err) {
        console.error(err);
        alert(err?.message || "旅程の自動生成中にエラーが発生しました。");
    } finally {
        if (btnBuild) {
            btnBuild.disabled = false;
            btnBuild.textContent = "選択したスポットで実行可能な旅程を作成 ➔";
        }
    }
}
 
function renderItinerary(data, startLoc) {
    const m = data.metrics || {};
    const metricsArea = document.getElementById('metrics-area');
    if (metricsArea) {
        const budgetBadge = m.budget_satisfied === false
            ? '<span class="badge badge-warn">⚠️ 予算上限を超過</span>'
            : '<span class="badge badge-ok">✔ 予算内</span>';
        const timeBadge = m.time_satisfied === false
            ? '<span class="badge badge-warn">⚠️ 時間上限を超過</span>'
            : '<span class="badge badge-ok">✔ 時間内</span>';
        const hoursBadge = m.hours_satisfied === false
            ? '<span class="badge badge-warn">⚠️ 営業時間外の訪問あり</span>'
            : '<span class="badge badge-ok">✔ 営業時間内</span>';
 
        metricsArea.innerHTML = `
            <div class="perf-card cost-style">
                <div class="perf-title">💰 コスパ節約額 Δg（移動費用）</div>
                <div class="perf-num">¥${(m.cost_saved_yen || 0).toLocaleString()}</div>
                <div class="perf-formula">区間ごとの個別手配運賃想定 − 本プランの移動費用実額</div>
            </div>
            <div class="perf-card time-style">
                <div class="perf-title">⏱️ タイパ節約時間 Δt（移動時間）</div>
                <div class="perf-num">${m.time_saved_minutes || 0}分</div>
                <div class="perf-formula">最悪の巡回順で移動した場合の想定時間 − 本プランの移動時間</div>
            </div>
            <div class="perf-card" style="grid-column: 1 / -1;">
                <div class="perf-title">🎯 制約充足状況（式3〜5） ／ 体験ピーク適合度 f1 = ${(m.f1_satisfaction ?? 0).toFixed(3)}</div>
                <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:6px;">${budgetBadge}${timeBadge}${hoursBadge}</div>
            </div>
        `;
    }
 
    const tlArea = document.getElementById('timeline-area');
    if (!tlArea) return;
    tlArea.innerHTML = '';
 
    const daySection = document.createElement('div');
    daySection.className = 'day-section';
    daySection.innerHTML = `
        <div class="day-section-header">
            <div class="day-section-title">📅 当日</div>
            <div class="day-section-sub">費用: ¥${(m.total_cost || 0).toLocaleString()} / 所要: ${m.total_time_minutes || 0}分</div>
        </div>
        <div class="day-timeline"></div>
    `;
 
    const timelineInner = daySection.querySelector('.day-timeline');
    const [startH, startM] = (data.start_time || "09:00").split(':').map(Number);
    let currentMin = startH * 60 + startM;
    const waypointsForMap = [];
 
    (data.itinerary || []).forEach((item) => {
        const startTimeStr = formatMinutesToHHMM(currentMin);
 
        if (item.type === 'transit') {
            currentMin += item.duration_minutes;
            const endTimeStr = formatMinutesToHHMM(currentMin);
 
            const transitDiv = document.createElement('div');
            transitDiv.className = 'tl-transit';
            transitDiv.textContent = `🚃 電車・バスで移動：${item.from_name} → ${item.to_name}（約${item.duration_minutes}分）（AI検証済み）`;
            timelineInner.appendChild(transitDiv);
 
        } else if (item.type === 'spot') {
            const placeName = item.place.name;
            waypointsForMap.push(placeName);
 
            currentMin += item.stay_minutes;
            const endTimeStr = formatMinutesToHHMM(currentMin);
            const isCustomWp = item.is_custom_waypoint;
 
            const spotDiv = document.createElement('div');
            spotDiv.className = 'timeline-item';
 
            const hoursWarning = item.hours_satisfied === false
                ? '<span class="badge badge-warn">⚠️ 営業時間外の可能性</span>' : '';
            // 営業時間がOSMの実データではなく種別からの推定値の場合、確信度が低いことを示す
            // （実データが取れていれば hours_source は "osm" になる）。
            const hoursEstimatedNote = item.hours_source && item.hours_source !== 'osm'
                ? '<span class="badge badge-hours" title="実際の営業時間データが取得できなかったため、種別からの推定値を使用しています">営業時間は推定値</span>' : '';
 
            spotDiv.innerHTML = `
                <div class="tl-time">${startTimeStr}</div>
                <div class="tl-badge"></div>
                <div class="tl-card ${item.hours_satisfied === false ? 'warning' : 'info'}">
                    <div class="tl-card-head">
                        <span class="tl-card-name">${escapeHtml(placeName)}</span>
                        <span>
                            ${item.is_peak ? '<span class="badge badge-peak">メインピーク</span>' : '<span class="badge badge-circuit">周遊</span>'}
                            ${isCustomWp ? '<span class="badge badge-custom">追加スポット</span>' : ''}
                            ${hoursWarning}
                            ${hoursEstimatedNote}
                        </span>
                    </div>
                    <div class="tl-card-sub">滞在時間 約${item.stay_minutes}分（〜${endTimeStr}）</div>
                </div>
            `;
            timelineInner.appendChild(spotDiv);
        }
    });
 
    // Googleマップ連携ボタン
    // final_destination はバックエンドが実際に計算した終着点（到着地点指定があればそれ、
    // なければ往復=出発地／片道=最終訪問地）。以前はフロント側でtrip_typeだけから
    // 推測していたが、到着地点を明示指定できるようにしたのに合わせてバックエンドの
    // 計算結果をそのまま使うようにする。
    const destName = data.final_destination || ((data.trip_type === "round_trip") ? startLoc : (waypointsForMap[waypointsForMap.length - 1] || startLoc));
    const waypointsParam = waypointsForMap.filter(name => name !== destName).map(encodeURIComponent).join('|');
    const mapUrl = `https://www.google.com/maps/dir/?api=1&origin=${encodeURIComponent(startLoc)}&destination=${encodeURIComponent(destName)}&waypoints=${waypointsParam}`;
 
    const mapBtn = document.createElement('a');
    mapBtn.className = 'btn-day-map';
    mapBtn.href = mapUrl;
    mapBtn.target = '_blank';
    mapBtn.rel = 'noopener';
    mapBtn.textContent = '🗺️ 当日のルートをGoogleマップで開く';
    daySection.appendChild(mapBtn);
 
    tlArea.appendChild(daySection);
}
 
/* --- 7. 動的リプランニング機能 --- */
const replanToggle = document.getElementById('replan-toggle');
if (replanToggle) {
    replanToggle.addEventListener('change', () => {
        const body = document.getElementById('replan-body');
        if (body) body.style.display = replanToggle.checked ? 'block' : 'none';
        if (replanToggle.checked) {
            const now = new Date();
            const timeInput = document.getElementById('replan-time');
            if (timeInput) {
                timeInput.value = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
            }
        }
    });
}
 
document.getElementById('btn-replan')?.addEventListener('click', () => {
    const curLoc = document.getElementById('replan-location')?.value || '現在地';
    const curTime = document.getElementById('replan-time')?.value || '12:00';
    
    const output = document.getElementById('replan-output');
    if (output) output.style.display = 'block';
 
    const diffBox = document.getElementById('replan-diff');
    if (diffBox) {
        diffBox.innerHTML = `
            <div><strong>状況：</strong>予定変更（${curTime}時点／現在地：${curLoc}）</div>
            <div class="replan-diff" style="margin-top:8px;">
                <div><strong>継続して訪問：</strong> <span class="kept">後半ピークスポット、温泉街</span></div>
            </div>
        `;
    }
});
 
/* --- 8. 口コミ評価機能 --- */
function initStarRating() {
    const ratingBox = document.getElementById('star-rating');
    if (!ratingBox) return;
 
    let selectedStar = 0;
    ratingBox.addEventListener('click', (e) => {
        const span = e.target.closest('span');
        if (span) {
            selectedStar = parseInt(span.dataset.v, 10);
            ratingBox.querySelectorAll('span').forEach(s => {
                const val = parseInt(s.dataset.v, 10);
                s.classList.toggle('active', val <= selectedStar);
            });
        }
    });
 
    const btnSubmitReview = document.getElementById('btn-submit-review');
    btnSubmitReview?.addEventListener('click', async () => {
        if (selectedStar === 0) {
            alert("評価を選択してください。");
            return;
        }
 
        // 今後のデータ活用のため、★評価と口コミ本文に加えて、どの旅程に対する評価かが
        // 後から分かるよう、直近に生成した旅程の要約情報も一緒に送る。
        const reviewText = document.getElementById('review-text')?.value?.trim() || '';
        const metrics = lastGeneratedItineraryData?.metrics || {};
        const payload = {
            rating: selectedStar,
            review_text: reviewText,
            final_destination: lastGeneratedItineraryData?.final_destination || '',
            start_location: document.getElementById('start-location')?.value?.trim() || '',
            transport_mode: document.getElementById('transport-mode')?.value || '',
            trip_type: lastGeneratedItineraryData?.trip_type || '',
            member_count: lastGeneratedItineraryData?.member_count || null,
            total_cost: metrics.total_cost ?? null,
            total_time_minutes: metrics.total_time_minutes ?? null
        };
 
        if (btnSubmitReview) btnSubmitReview.disabled = true;
        try {
            const res = await fetch(`${API_BASE_URL}/submit_review`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!res.ok) {
                let detail = "評価の送信に失敗しました。";
                try {
                    const errBody = await res.json();
                    if (errBody?.detail) detail = errBody.detail;
                } catch (_) { /* JSONでなければ既定メッセージのまま */ }
                throw new Error(detail);
            }
            alert("評価を送信しました。ご協力ありがとうございます。");
            // 送信後はフォームをリセットし、二重送信で同じ内容が重複保存されるのを防ぐ
            selectedStar = 0;
            ratingBox.querySelectorAll('span').forEach(s => s.classList.remove('active'));
            const reviewTextEl = document.getElementById('review-text');
            if (reviewTextEl) reviewTextEl.value = '';
        } catch (err) {
            console.error(err);
            alert(err?.message || "評価の送信中にエラーが発生しました。");
        } finally {
            if (btnSubmitReview) btnSubmitReview.disabled = false;
        }
    });
}
 
/* --- お気に入り機能 --- */
function saveToFavorites() {
    if (!lastGeneratedItineraryData) return;
    const favs = JSON.parse(localStorage.getItem('my_favorite_itineraries') || '[]');
    const area = document.getElementById('target-area')?.value?.trim() || lastGeneratedItineraryData.final_destination || '旅程';
    const startTime = document.getElementById('start-time')?.value || '09:00';
    const title = `${area} 旅程 (${startTime}発)`;
 
    favs.push({ title, date: '2026/8/7', data: lastGeneratedItineraryData });
    localStorage.setItem('my_favorite_itineraries', JSON.stringify(favs));
    alert('⭐ この旅程をお気に入りに保存しました！');
    loadFavorites();
}
 
function loadFavorites() {
    const favs = JSON.parse(localStorage.getItem('my_favorite_itineraries') || '[]');
    const container = document.getElementById('favorites-list');
    if (!container) return;
 
    container.innerHTML = favs.map((f, i) => `
        <div style="background:#f8fafc; padding:10px 14px; border-radius:8px; border:1px solid #e2e8f0; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
            <div>
                <strong>${escapeHtml(f.title)}</strong>
                <span style="color:#64748b; font-size:0.8rem; display:block;">保存日: ${f.date}</span>
            </div>
            <div style="display:flex; gap:6px;">
                <button type="button" class="btn-restore-fav" data-index="${i}" style="background:#2563eb; color:white; border:none; border-radius:4px; padding:6px 12px; font-weight:bold; cursor:pointer; font-size:0.85rem;">📂 このしおりを表示</button>
                <button type="button" class="btn-remove-fav" data-index="${i}" style="background:#ef4444; color:white; border:none; border-radius:4px; padding:6px 10px; cursor:pointer; font-size:0.85rem;">削除</button>
            </div>
        </div>
    `).join('');
}
 
function formatMinutesToHHMM(totalMinutes) {
    // 日付をまたぐ移動・滞在（例: 900分の移動で翌日にずれ込む場合）でも「00:00」だけが表示されて
    // 前日の続きなのか当日なのか分からなくなる問題を避けるため、日をまたいだ分だけ「+N日」を付ける。
    const dayOffset = Math.floor(totalMinutes / 1440);
    const h = Math.floor((totalMinutes % 1440) / 60);
    const m = totalMinutes % 60;
    const clock = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
    return dayOffset > 0 ? `${clock}（+${dayOffset}日）` : clock;
}
 
function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}