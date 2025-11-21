from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import serial
import serial.tools.list_ports
import threading
import time
from datetime import datetime
import math
import os
import socket
import ssl

app = Flask(__name__)
CORS(app)

# ============================================
# ARDUINO AUTO-DETECTION
# ============================================
def find_arduino():
    """Automatically find Arduino COM port"""
    print("🔍 Searching for Arduino...")
    ports = serial.tools.list_ports.comports()
    
    for port in ports:
        print(f"   Found device: {port.device} - {port.description}")
        if any(keyword in port.description.upper() for keyword in ['ARDUINO', 'CH340', 'USB', 'SERIAL', 'COM']):
            for attempt in range(3):
                try:
                    ser = serial.Serial(port.device, 9600, timeout=1, write_timeout=1)
                    time.sleep(2.5)
                    print(f"✅ Arduino connected on {port.device}")
                    return ser
                except serial.SerialException as e:
                    if "PermissionError" in str(e) or "Access is denied" in str(e):
                        print(f"   ⚠️  Permission denied on {port.device}")
                        print(f"   💡 Solution: Close Arduino IDE and any serial monitors!")
                        time.sleep(1)
                    else:
                        print(f"   ❌ Attempt {attempt+1} failed: {e}")
                    if attempt < 2:
                        time.sleep(1)
                except Exception as e:
                    print(f"   ❌ Error: {e}")
                    break
    
    print("\n⚠️  Arduino not found. System will work without sensor.")
    return None

arduino = find_arduino()

# ============================================
# GET LOCAL IP ADDRESS
# ============================================
def get_local_ip():
    """Get the local IP address of this machine"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except:
        return "localhost"

# ============================================
# CREATE SELF-SIGNED SSL CERTIFICATE
# ============================================
def create_ssl_cert():
    """Create a self-signed SSL certificate for HTTPS"""
    import subprocess
    
    cert_file = 'cert.pem'
    key_file = 'key.pem'
    
    # Check if certificates already exist
    if os.path.exists(cert_file) and os.path.exists(key_file):
        print("✅ SSL certificates found")
        return cert_file, key_file
    
    print("🔐 Creating SSL certificate for HTTPS...")
    try:
        # Create self-signed certificate using openssl
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:4096',
            '-keyout', key_file, '-out', cert_file,
            '-days', '365', '-nodes',
            '-subj', '/CN=localhost'
        ], check=True, capture_output=True)
        print("✅ SSL certificates created")
        return cert_file, key_file
    except:
        print("⚠️  OpenSSL not found. Install OpenSSL or use HTTP (GPS won't work on phone)")
        return None, None

# Try to create SSL certificates
cert_file, key_file = create_ssl_cert()
use_https = cert_file is not None and key_file is not None

# ============================================
# DATA STORAGE
# ============================================
dustbins = {
    "1": {
        "name": "Bin 1 - MITS College", 
        "location": "Bhujabal, Rayagada", 
        "lat": 19.2461, 
        "lon": 83.4462, 
        "fill": 0, 
        "status": "EMPTY"
    }
}

truck = {
    "lat": None,
    "lon": None,
    "timestamp": None
}


# ============================================
# ROUTE OPTIMIZATION - TSP ALGORITHM
# ============================================
# def calculate_distance(lat1, lon1, lat2, lon2):
#     """Calculate distance between two GPS points (Haversine formula)"""
#     R = 6371
#     dlat = math.radians(lat2 - lat1)
#     dlon = math.radians(lon2 - lon1)
#     a = (math.sin(dlat/2)**2 + 
#          math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * 
#          math.sin(dlon/2)**2)
#     c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
#     distance = R * c
#     return distance

# def optimize_route_tsp(truck_pos, bins):
#     """Traveling Salesman Problem - Nearest Neighbor Algorithm"""
#     if not bins:
#         return [], 0
#     route = []
#     current = truck_pos
#     remaining = bins.copy()
#     total_distance = 0
#     while remaining:
#         nearest = min(remaining, key=lambda b: calculate_distance(
#             current['lat'], current['lon'], b['lat'], b['lon']
#         ))
#         dist = calculate_distance(
#             current['lat'], current['lon'], 
#             nearest['lat'], nearest['lon']
#         )
#         total_distance += dist
#         route.append(nearest)
#         remaining.remove(nearest)
#         current = nearest
#     return route, total_distance

def calculate_distance(lat1, lon1, lat2, lon2):
    """
    Calculate geodesic distance (in kilometers) between two GPS points
    using the Haversine formula.
    """
    R = 6371.0  # Earth's radius in km

    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (math.sin(dlat / 2)**2 +
         math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2)

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


def optimize_route_tsp(truck_pos, bins):
    """
    Solve a basic TSP using the Nearest Neighbor heuristic.
    
    truck_pos: { "lat": float, "lon": float }
    bins: [ { "lat": float, "lon": float, ... }, ... ]

    Returns:
        - ordered list of bins (optimal path)
        - total travel distance in kilometers
    """
    if not bins:
        return [], 0.0

    remaining = bins[:]  # clone list to avoid mutation
    route = []
    current = truck_pos
    total_distance = 0.0

    while remaining:
        # Find nearest bin
        nearest = min(
            remaining,
            key=lambda b: calculate_distance(
                current["lat"], current["lon"], b["lat"], b["lon"]
            )
        )

        # Add distance to route
        dist = calculate_distance(
            current["lat"], current["lon"], nearest["lat"], nearest["lon"]
        )
        total_distance += dist

        # Move to next bin
        route.append(nearest)
        remaining.remove(nearest)
        current = nearest

    return route, total_distance

# ============================================
# ARDUINO READER THREAD
# ============================================
def read_arduino_data():
    """Continuously read sensor data from Arduino"""
    print("🔄 Arduino reader thread started")
    while True:
        if arduino:
            try:
                if arduino.in_waiting > 0:
                    line = arduino.readline().decode('utf-8', errors='ignore').strip()
                    if line.startswith("DATA|"):
                        parts = line.replace("DATA|", "").split("|")
                        if len(parts) >= 1:
                            fill_level = float(parts[0])
                            if "1" in dustbins:
                                dustbins["1"]["fill"] = fill_level
                                if fill_level >= 80:
                                    dustbins["1"]["status"] = "FULL"
                                elif fill_level >= 50:
                                    dustbins["1"]["status"] = "MEDIUM"
                                else:
                                    dustbins["1"]["status"] = "EMPTY"
                            print(f"📊 Sensor: Bin 1 = {fill_level:.0f}%")
            except Exception as e:
                pass
        time.sleep(0.5)

if arduino:
    reader_thread = threading.Thread(target=read_arduino_data, daemon=True)
    reader_thread.start()

# ============================================
# API ENDPOINTS
# ============================================

@app.route('/')
def home():
    """API status page"""
    local_ip = get_local_ip()
    protocol = "https" if use_https else "http"
    return jsonify({
        "status": "running",
        "arduino": "connected" if arduino else "not connected",
        "https": use_https,
        "local_ip": local_ip,
        "endpoints": {
            "dashboard": f"{protocol}://{local_ip}:5000/dashboard",
            "gps_tracker": f"{protocol}://{local_ip}:5000/gps"
        }
    })

@app.route('/dashboard')
def dashboard():
    """Serve dashboard HTML"""
    try:
        return send_file('dashboard.html')
    except:
        return jsonify({"error": "dashboard.html not found"}), 404

@app.route('/gps')
def gps_page():
    """Mobile GPS tracker page"""
    html = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GPS Tracker</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 1rem;
        }
        .container {
            background: white;
            border-radius: 20px;
            padding: 2rem;
            max-width: 400px;
            width: 100%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }
        h1 {
            color: #667eea;
            margin-bottom: 1rem;
            text-align: center;
            font-size: 1.8rem;
        }
        .status {
            background: #f3f4f6;
            padding: 1.5rem;
            border-radius: 12px;
            margin: 1rem 0;
            min-height: 140px;
        }
        .coords {
            font-family: 'Courier New', monospace;
            font-size: 0.9rem;
            color: #374151;
            margin: 0.5rem 0;
        }
        button {
            width: 100%;
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            border: none;
            padding: 1rem;
            border-radius: 12px;
            font-size: 1.1rem;
            font-weight: bold;
            cursor: pointer;
            margin: 0.5rem 0;
        }
        button:active { transform: scale(0.98); }
        button:disabled { background: #9ca3af; }
        .btn-stop { background: linear-gradient(135deg, #ef4444, #dc2626) !important; }
        .success { color: #10b981; font-weight: bold; }
        .error { color: #ef4444; font-weight: bold; }
        .warning { color: #f59e0b; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Truck GPS Tracker</h1>
        
        <button id="startBtn">Start GPS Tracking</button>
        <button class="btn-stop" id="stopBtn">Stop Tracking</button>
        
        <div class="status" id="status">
            <div class="warning">Ready to start...</div>
        </div>
    </div>

    <script>
        var watchId = null;
        var isTracking = false;
        var updateCount = 0;

        document.getElementById('startBtn').addEventListener('click', startTracking);
        document.getElementById('stopBtn').addEventListener('click', stopTracking);

        function startTracking() {
            if (isTracking) return;
            if (!navigator.geolocation) {
                document.getElementById('status').innerHTML = 
                    '<div class="error">GPS not supported</div>';
                return;
            }

            document.getElementById('status').innerHTML = 
                '<div class="warning">Getting GPS...</div>';
            document.getElementById('startBtn').disabled = true;
            
            watchId = navigator.geolocation.watchPosition(
                updatePosition,
                handleError,
                { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
            );
            isTracking = true;
        }

        function stopTracking() {
            if (watchId) {
                navigator.geolocation.clearWatch(watchId);
                watchId = null;
            }
            isTracking = false;
            updateCount = 0;
            document.getElementById('startBtn').disabled = false;
            document.getElementById('status').innerHTML = 
                '<div class="warning">Tracking stopped</div>';
        }

        function updatePosition(pos) {
            updateCount++;
            var lat = pos.coords.latitude;
            var lon = pos.coords.longitude;
            var acc = pos.coords.accuracy.toFixed(0);

            fetch('/api/truck', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lat: lat, lon: lon })
            }).catch(function(e) { console.error(e); });

            document.getElementById('status').innerHTML = 
                '<div class="success">TRACKING ACTIVE</div>' +
                '<div class="coords">Lat: ' + lat.toFixed(6) + '</div>' +
                '<div class="coords">Lon: ' + lon.toFixed(6) + '</div>' +
                '<div class="coords">Accuracy: +/- ' + acc + 'm</div>' +
                '<div class="coords">Updates: ' + updateCount + '</div>';
        }

        function handleError(err) {
            var msg = 'GPS Error: ';
            if (err.code === 1) msg += 'Permission denied';
            else if (err.code === 2) msg += 'Position unavailable';
            else if (err.code === 3) msg += 'Timeout';
            else msg += err.message;
            
            document.getElementById('status').innerHTML = 
                '<div class="error">' + msg + '</div>';
            stopTracking();
        }
    </script>
</body>
</html>'''
    return html

@app.route('/api/dustbins', methods=['GET'])
def get_dustbins():
    return jsonify(dustbins)

@app.route('/api/dustbins', methods=['POST'])
def add_dustbin():
    try:
        data = request.json
        new_id = str(max([int(k) for k in dustbins.keys()], default=0) + 1)
        dustbins[new_id] = {
            "name": data.get('name', f'Bin {new_id}'),
            "location": data.get('location', 'Unknown'),
            "lat": float(data.get('lat', 19.31)),
            "lon": float(data.get('lon', 84.02)),
            "fill": 0,
            "status": "EMPTY"
        }
        return jsonify({"message": "Added", "id": new_id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/dustbins/<bin_id>', methods=['DELETE'])
def delete_dustbin(bin_id):
    if bin_id in dustbins:
        del dustbins[bin_id]
        return jsonify({"message": "Deleted"}), 200
    return jsonify({"error": "Not found"}), 404

@app.route('/api/truck', methods=['GET'])
def get_truck():
    return jsonify(truck)

@app.route('/api/truck', methods=['POST'])
def update_truck():
    try:
        data = request.json
        truck["lat"] = float(data.get('lat', truck["lat"]))
        truck["lon"] = float(data.get('lon', truck["lon"]))
        truck["timestamp"] = datetime.now().isoformat()
        print(f"📍 Truck: ({truck['lat']:.6f}, {truck['lon']:.6f})")
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/optimize', methods=['GET'])
def optimize_route():
    try:
        # ❗ Check if we have live GPS
        if truck["lat"] is None or truck["lon"] is None:
            return jsonify({
                "error": "Truck GPS not received yet. Open /gps on your phone and press Start Tracking."
            }), 400

        # Get all bins >= 50% for collection
        bins_to_collect = [
            {"id": bid, "name": b["name"], "lat": b["lat"], "lon": b["lon"], "fill": b["fill"]}
            for bid, b in dustbins.items() if b["fill"] >= 50
        ]

        if not bins_to_collect:
            return jsonify({
                "message": "No bins need collection",
                "optimal_distance": 0,
                "route": []
            })

        # 🚛➡️ Now route optimization uses *live phone GPS*
        optimal_route, optimal_distance = optimize_route_tsp(truck, bins_to_collect)

        # Random route for comparison
        import random
        random_bins = bins_to_collect.copy()
        random.shuffle(random_bins)

        random_distance = 0
        if len(random_bins) > 1:
            for i in range(len(random_bins) - 1):
                random_distance += calculate_distance(
                    random_bins[i]["lat"], random_bins[i]["lon"],
                    random_bins[i+1]["lat"], random_bins[i+1]["lon"]
                )

        optimal_fuel = optimal_distance / 12
        fuel_cost = optimal_fuel * 100

        savings = (random_distance/12 - optimal_fuel) * 100 if random_distance > 0 else 0
        efficiency = ((random_distance - optimal_distance) / random_distance * 100) if random_distance > 0 else 0

        return jsonify({
            "optimal_distance": round(optimal_distance, 2),
            "random_distance": round(random_distance, 2),
            "optimal_fuel": round(optimal_fuel, 2),
            "optimal_cost": round(fuel_cost),
            "savings": round(savings),
            "efficiency_gain": round(efficiency),
            "route": [{"id": b["id"], "name": b["name"]} for b in optimal_route]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/predictions', methods=['GET'])
def get_predictions():
    preds = []
    for bid, b in dustbins.items():
        if b["fill"] < 100:
            days = (100 - b["fill"]) / 15
            urgency = "CRITICAL" if b["fill"] >= 80 else "HIGH" if b["fill"] >= 60 else "MEDIUM"
            preds.append({
                "bin_id": bid,
                "name": b["name"],
                "current_fill": b["fill"],
                "days_until_full": round(days, 1),
                "urgency": urgency
            })
    return jsonify(sorted(preds, key=lambda x: x["days_until_full"]))

# ============================================
# START SERVER
# ============================================
if __name__ == '__main__':
    local_ip = get_local_ip()
    protocol = "https" if use_https else "http"
    
    print("\n" + "="*70)
    print("🌍 ECO-BINGO BACKEND SERVER")
    print("="*70)
    print(f"Arduino: {'✅ Connected' if arduino else '⚠️  Not Connected'}")
    print(f"HTTPS: {'✅ Enabled (GPS will work!)' if use_https else '⚠️  Disabled (Install OpenSSL)'}")
    print(f"\n📱 PHONE GPS: {protocol}://{local_ip}:5000/gps")
    print(f"🖥️  LAPTOP: {protocol}://localhost:5000/dashboard")
    if use_https:
        print(f"\n⚠️  Accept security warning on phone (self-signed certificate)")
    print("="*70 + "\n")
    
    if use_https:
        app.run(debug=True, host='0.0.0.0', port=5000, 
                ssl_context=(cert_file, key_file), use_reloader=False)
    else:
        app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)