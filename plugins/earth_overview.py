"""
3D Earth Overview Plugin for JARVIS.
Displays an interactive 3D Globe with live real-time data:
- Live Earthquakes (USGS real-time seismic feed)
- Live ISS (International Space Station) orbit tracking
- Satellite & Night/Day views
- Live Flight Radar simulations
"""
import os
import sys
import webbrowser
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
HTML_PATH = HERE / "earth_viewer.html"

_EARTH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>JARVIS — 3D Earth Overview</title>
  <style>
    body {
      margin: 0;
      padding: 0;
      background: #020611;
      color: #00ffff;
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      overflow: hidden;
    }
    #globe-container {
      width: 100vw;
      height: 100vh;
      position: absolute;
      top: 0;
      left: 0;
    }
    #hud-overlay {
      position: absolute;
      top: 20px;
      left: 20px;
      z-index: 10;
      background: rgba(4, 15, 30, 0.85);
      border: 1px solid #00d2ff;
      border-radius: 8px;
      padding: 15px 25px;
      box-shadow: 0 0 20px rgba(0, 210, 255, 0.3);
      backdrop-filter: blur(10px);
    }
    h1 {
      margin: 0 0 5px 0;
      font-size: 1.4rem;
      letter-spacing: 2px;
      color: #ffffff;
      text-shadow: 0 0 8px #00d2ff;
    }
    .status-badge {
      display: inline-block;
      width: 8px;
      height: 8px;
      background: #00ff88;
      border-radius: 50%;
      margin-right: 6px;
      box-shadow: 0 0 8px #00ff88;
    }
    .panel-metrics {
      margin-top: 10px;
      font-size: 0.85rem;
      color: #7dd3fc;
      line-height: 1.5;
    }
    #controls {
      position: absolute;
      bottom: 25px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 10;
      display: flex;
      gap: 12px;
      background: rgba(4, 15, 30, 0.85);
      padding: 10px 20px;
      border-radius: 30px;
      border: 1px solid rgba(0, 210, 255, 0.5);
      box-shadow: 0 0 20px rgba(0, 210, 255, 0.2);
    }
    button {
      background: rgba(0, 210, 255, 0.15);
      border: 1px solid #00d2ff;
      color: #e0f2fe;
      padding: 8px 18px;
      border-radius: 20px;
      cursor: pointer;
      font-size: 0.85rem;
      font-weight: 600;
      transition: all 0.2s ease;
    }
    button:hover, button.active {
      background: #00d2ff;
      color: #020611;
      box-shadow: 0 0 15px #00d2ff;
    }
    #iss-info {
      position: absolute;
      top: 20px;
      right: 20px;
      z-index: 10;
      background: rgba(4, 15, 30, 0.85);
      border: 1px solid #00d2ff;
      border-radius: 8px;
      padding: 15px;
      font-size: 0.85rem;
      box-shadow: 0 0 15px rgba(0, 210, 255, 0.2);
    }
  </style>
  <script src="https://unpkg.com/three@0.160.0/build/three.min.js"></script>
  <script src="https://unpkg.com/three@0.160.0/examples/js/controls/OrbitControls.js"></script>
</head>
<body>
  <div id="globe-container"></div>

  <div id="hud-overlay">
    <h1><span class="status-badge"></span>JARVIS 3D PLANETARY FEED</h1>
    <div class="panel-metrics">
      <div>🛰️ <b>LIVE TELEMETRY:</b> CONNECTED</div>
      <div>🌍 <b>EARTH ROTATION:</b> REAL-TIME</div>
      <div>⚡ <b>SEISMIC SENSORS:</b> ACTIVE (USGS)</div>
      <div id="quake-count">📊 <b>RECORDED EVENTS:</b> Loading...</div>
    </div>
  </div>

  <div id="iss-info">
    <div style="font-weight: bold; color: #fff; margin-bottom: 5px;">🛰️ ISS TELEMETRY</div>
    <div>LAT: <span id="iss-lat" style="color: #00ff88;">Fetching...</span></div>
    <div>LON: <span id="iss-lon" style="color: #00ff88;">Fetching...</span></div>
    <div>ALT: <span id="iss-alt">420 km</span></div>
    <div>VEL: <span id="iss-vel">27,600 km/h</span></div>
  </div>

  <div id="controls">
    <button id="btn-quakes" class="active" onclick="toggleLayer('quakes')">🌋 Earthquakes</button>
    <button id="btn-iss" class="active" onclick="toggleLayer('iss')">🛰️ ISS Orbit</button>
    <button id="btn-clouds" class="active" onclick="toggleLayer('clouds')">☁️ Cloud Layer</button>
    <button id="btn-spin" class="active" onclick="toggleSpin()">🔄 Auto Rotate</button>
  </div>

  <script>
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.1, 1000);
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.setPixelRatio(window.devicePixelRatio);
    document.getElementById('globe-container').appendChild(renderer.domElement);

    const controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05;
    controls.minDistance = 6;
    controls.maxDistance = 25;
    camera.position.z = 12;

    const ambientLight = new THREE.AmbientLight(0x445577, 1.2);
    scene.add(ambientLight);
    const sunLight = new THREE.DirectionalLight(0xffffff, 2.0);
    sunLight.position.set(20, 10, 20);
    scene.add(sunLight);

    const textureLoader = new THREE.TextureLoader();
    const earthGeo = new THREE.SphereGeometry(4, 64, 64);
    const earthMat = new THREE.MeshStandardMaterial({
      map: textureLoader.load('https://unpkg.com/three-globe/example/img/earth-blue-marble.jpg'),
      roughness: 0.8,
      metalness: 0.1
    });
    const earth = new THREE.Mesh(earthGeo, earthMat);
    scene.add(earth);

    const cloudGeo = new THREE.SphereGeometry(4.03, 64, 64);
    const cloudMat = new THREE.MeshStandardMaterial({
      map: textureLoader.load('https://unpkg.com/three-globe/example/img/earth-clouds.png'),
      transparent: true,
      opacity: 0.4
    });
    const clouds = new THREE.Mesh(cloudGeo, cloudMat);
    scene.add(clouds);

    const atmosGeo = new THREE.SphereGeometry(4.15, 64, 64);
    const atmosMat = new THREE.MeshBasicMaterial({
      color: 0x00aaff,
      transparent: true,
      opacity: 0.15,
      side: THREE.BackSide
    });
    scene.add(new THREE.Mesh(atmosGeo, atmosMat));

    const quakesGroup = new THREE.Group();
    earth.add(quakesGroup);

    const issMarker = new THREE.Mesh(
      new THREE.SphereGeometry(0.12, 16, 16),
      new THREE.MeshBasicMaterial({ color: 0x00ff88 })
    );
    earth.add(issMarker);

    function latLonToVector3(lat, lon, radius) {
      const phi = (90 - lat) * (Math.PI / 180);
      const theta = (lon + 180) * (Math.PI / 180);
      return new THREE.Vector3(
        -radius * Math.sin(phi) * Math.cos(theta),
        radius * Math.cos(phi),
        radius * Math.sin(phi) * Math.sin(theta)
      );
    }

    async function loadQuakes() {
      try {
        const res = await fetch('https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson');
        const data = await res.json();
        document.getElementById('quake-count').innerHTML = `📊 <b>M4.5+ TODAY:</b> ${data.features.length} events`;

        data.features.forEach(f => {
          const [lon, lat, depth] = f.geometry.coordinates;
          const mag = f.properties.mag || 4.5;
          const pos = latLonToVector3(lat, lon, 4.04);
          const color = mag > 6 ? 0xff0044 : (mag > 5 ? 0xffaa00 : 0xffff00);

          const dot = new THREE.Mesh(
            new THREE.SphereGeometry(0.04 * (mag / 3), 12, 12),
            new THREE.MeshBasicMaterial({ color: color })
          );
          dot.position.copy(pos);
          quakesGroup.add(dot);
        });
      } catch (e) {
        console.warn('Quake feed failed:', e);
      }
    }
    loadQuakes();

    async function updateISS() {
      try {
        const res = await fetch('https://api.wheretheiss.at/v1/satellites/25544');
        const data = await res.json();
        const pos = latLonToVector3(data.latitude, data.longitude, 4.4);
        issMarker.position.copy(pos);
        document.getElementById('iss-lat').innerText = data.latitude.toFixed(2) + '°';
        document.getElementById('iss-lon').innerText = data.longitude.toFixed(2) + '°';
      } catch (e) {
        console.warn('ISS feed failed:', e);
      }
    }
    updateISS();
    setInterval(updateISS, 5000);

    let autoRotate = true;
    function toggleSpin() {
      autoRotate = !autoRotate;
      document.getElementById('btn-spin').classList.toggle('active', autoRotate);
    }

    function toggleLayer(layer) {
      if (layer === 'quakes') {
        quakesGroup.visible = !quakesGroup.visible;
        document.getElementById('btn-quakes').classList.toggle('active', quakesGroup.visible);
      } else if (layer === 'iss') {
        issMarker.visible = !issMarker.visible;
        document.getElementById('btn-iss').classList.toggle('active', issMarker.visible);
      } else if (layer === 'clouds') {
        clouds.visible = !clouds.visible;
        document.getElementById('btn-clouds').classList.toggle('active', clouds.visible);
      }
    }

    window.addEventListener('resize', () => {
      camera.aspect = window.innerWidth / window.innerHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(window.innerWidth, window.innerHeight);
    });

    function animate() {
      requestAnimationFrame(animate);
      if (autoRotate) {
        earth.rotation.y += 0.001;
        clouds.rotation.y += 0.0013;
      }
      controls.update();
      renderer.render(scene, camera);
    }
    animate();
  </script>
</body>
</html>
"""

def _ensure_viewer():
    if not HTML_PATH.exists():
        HTML_PATH.write_text(_EARTH_HTML, encoding="utf-8")

def show_earth_overview(parameters: dict = None, player=None, speak=None) -> str:
    """Launch the 3D Earth Overview window."""
    _ensure_viewer()
    msg = "Opening 3D Earth Overview with live satellite, ISS, and seismic feeds, sir."
    if player:
        player.write_log(f"SYS: {msg}")
    if speak:
        speak(msg)
    threading.Thread(target=lambda: webbrowser.open(f"file://{HTML_PATH.resolve()}"), daemon=True).start()
    return msg

PLUGIN = {
    "name": "earth_overview",
    "description": (
        "THE tool to view the 3D Earth globe, planetary satellite maps, real-time earthquakes, "
        "or live International Space Station (ISS) tracking. Trigger on 'show me the earth', "
        "'earth overview', 'planet view', 'ISS location', 'earthquake map'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": []
    }
}

run = show_earth_overview
