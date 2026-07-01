# Provenance Guard - walkthrough demo script.
#
# HOW TO USE FOR YOUR VIDEO:
#   1. Terminal 1:  .\.venv\Scripts\python.exe app.py      (leave running)
#   2. Terminal 2:  .\demo.ps1                             (this script)
# It pauses between steps so you can talk. Press Enter to advance each step.
# Run  .\demo.ps1 -Pause:$false  to play straight through (no pauses).

param([bool]$Pause = $true)

$base = "http://127.0.0.1:5000"

function Step($title) {
    Write-Host ""
    Write-Host ("=" * 70) -ForegroundColor DarkCyan
    Write-Host $title -ForegroundColor Cyan
    Write-Host ("=" * 70) -ForegroundColor DarkCyan
    if ($Pause) { Read-Host "  (press Enter to run this step)" | Out-Null }
}

# ---------------------------------------------------------------------------
Step "1. Health check - is the server up?"
Invoke-RestMethod "$base/health" | ConvertTo-Json

# ---------------------------------------------------------------------------
Step "2. Submit CASUAL HUMAN writing -> expect 'human', HIGH confidence"
$humanText = "ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium in it and i was thirsty for like three hours after. my friend got the spicy version and said it was better. probably won't go back unless someone drags me there"
$rHuman = Invoke-RestMethod "$base/submit" -Method Post -ContentType "application/json" -Body (@{ text = $humanText; creator_id = "demo-human" } | ConvertTo-Json)
Write-Host ("attribution : {0}" -f $rHuman.attribution) -ForegroundColor Green
Write-Host ("confidence  : {0}" -f $rHuman.confidence)
Write-Host ("label       : {0}" -f $rHuman.label.text)
Write-Host ("phrase      : {0}" -f $rHuman.label.confidence_phrase)
Write-Host ("signals     : stat={0}  llm={1}  agreement={2}" -f $rHuman.signals.statistical.score, $rHuman.signals.llm_judge.score, $rHuman.agreement)

# ---------------------------------------------------------------------------
Step "3. Submit 'CLEARLY AI' writing -> signals DISAGREE -> 'uncertain', LOW confidence"
$aiText = "Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders must collaborate to ensure responsible deployment."
$rAI = Invoke-RestMethod "$base/submit" -Method Post -ContentType "application/json" -Body (@{ text = $aiText; creator_id = "demo-ai" } | ConvertTo-Json)
Write-Host ("attribution : {0}" -f $rAI.attribution) -ForegroundColor Yellow
Write-Host ("confidence  : {0}" -f $rAI.confidence)
Write-Host ("label       : {0}" -f $rAI.label.text)
Write-Host ("signals     : stat={0}  llm={1}  agreement={2}" -f $rAI.signals.statistical.score, $rAI.signals.llm_judge.score, $rAI.agreement)
Write-Host "  ^ The LLM says AI (0.9) but the statistics disagree, so the system stays honest and says 'uncertain'." -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
Step "4. APPEAL the human submission -> status flips to 'under_review'"
$appealBody = @{ content_id = $rHuman.content_id; creator_reasoning = "I wrote this myself from personal experience - please review." } | ConvertTo-Json
$rAppeal = Invoke-RestMethod "$base/appeal" -Method Post -ContentType "application/json" -Body $appealBody
$rAppeal | ConvertTo-Json

# ---------------------------------------------------------------------------
Step "5. Confirm the content is now under review"
Invoke-RestMethod "$base/content/$($rHuman.content_id)" | ConvertTo-Json

# ---------------------------------------------------------------------------
Step "6. The AUDIT LOG - every decision + the appeal, structured"
$log = Invoke-RestMethod "$base/log"
Write-Host ("total entries: {0}" -f $log.entries.Count)
$log.entries | Select-Object -First 4 type, content_id, attribution, confidence, statistical_score, llm_score, status | Format-Table -AutoSize

Write-Host ""
Write-Host "Demo complete." -ForegroundColor Green
