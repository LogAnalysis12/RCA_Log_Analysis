$ErrorActionPreference = "Stop"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker CLI is required." }
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker daemon is not running or is inaccessible." }
if (-not $env:NEO4J_PASSWORD) { throw "Set NEO4J_PASSWORD for the disposable test database." }
$image = if ($env:NEO4J_IMAGE) { $env:NEO4J_IMAGE } else { "neo4j:5" }
docker rm -f neo4j-test *> $null
try {
    docker run --name neo4j-test -p 17474:7474 -p 17687:7687 -e "NEO4J_AUTH=neo4j/$env:NEO4J_PASSWORD" -d $image | Out-Null
    $ready = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        docker exec neo4j-test cypher-shell -u neo4j -p $env:NEO4J_PASSWORD 'RETURN 1' *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "Neo4j did not become ready within 90 seconds." }
    $env:NEO4J_TEST_URI = "bolt://localhost:17687"
    $env:NEO4J_TEST_ALLOW_WIPE = "1"
    python -m pytest -m "neo4j or neo4j_real" -v
    if ($LASTEXITCODE -ne 0) { throw "Live Neo4j tests failed with exit code $LASTEXITCODE." }
}
finally { docker rm -f neo4j-test *> $null }
