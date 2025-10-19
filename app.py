import eventlet
eventlet.monkey_patch()

import os
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room
from flask_sqlalchemy import SQLAlchemy
import random

app = Flask(__name__)
app.config['SECRET_KEY'] = 'maze-game-secret-key'
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'maze.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app,
                    cors_allowed_origins="*",
                    async_mode='eventlet',
                    ping_timeout=60,
                    ping_interval=25)

PLAYER_COLORS = [
    {'color': '#9333EA', 'name': 'Black'},
    {'color': '#DC2626', 'name': 'Red'},
    {'color': '#2563EB', 'name': 'Blue'},
    {'color': '#16A34A', 'name': 'Green'},
    {'color': '#9333EA', 'name': 'Purple'},
    {'color': '#EAB308', 'name': 'Yellow'},
    {'color': '#EA580C', 'name': 'Orange'},
    {'color': '#EC4899', 'name': 'Pink'},
    {'color': '#14B8A6', 'name': 'Teal'},
    {'color': '#84CC16', 'name': 'Lime'},
]

sid_to_name = {}

game_state = {
    "players": {},
    "player_order": [],
    "is_running": False,
    "game_mode": "turn_based",
    "maze": None,
    "maze_size": 20,
    "current_position": None,
    "player_positions": {},
    "start_position": None,
    "end_position": None,
    "current_turn_index": 0,
    "moves_per_turn": 5,
    "moves_remaining": 0,
    "player_moves": {},
    "host_sid": None,
    "winner": None,
    "finished_players": []
}

GAME_ROOM = 'game_room'
HOST_ROOM = 'host_room'
PLAYER_ROOM = 'player_room'

class GameHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player_name = db.Column(db.String(100), nullable=False)
    games_won = db.Column(db.Integer, default=0)
    games_played = db.Column(db.Integer, default=0)
    total_points = db.Column(db.Integer, default=0)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/player')
def player():
    return render_template('player.html')

@app.route('/api/history')
def get_history():
    players = GameHistory.query.order_by(GameHistory.total_points.desc()).all()
    return jsonify([{
        'name': p.player_name,
        'games_won': p.games_won,
        'games_played': p.games_played,
        'total_points': p.total_points
    } for p in players])

def generate_maze(size):
    """Generate a random maze with GUARANTEED path from start to end"""
    maze = [[1 for _ in range(size)] for _ in range(size)]
    
    def carve_path(row, col):
        maze[row][col] = 0
        directions = [(0, 2), (2, 0), (0, -2), (-2, 0)]
        random.shuffle(directions)
        
        for dr, dc in directions:
            new_row, new_col = row + dr, col + dc
            if 0 < new_row < size - 1 and 0 < new_col < size - 1 and maze[new_row][new_col] == 1:
                maze[row + dr // 2][col + dc // 2] = 0
                carve_path(new_row, new_col)
    
    carve_path(1, 1)
    
    start_row, start_col = 1, 1
    end_row, end_col = size - 2, size - 2
    
    maze[start_row][start_col] = 0
    maze[end_row][end_col] = 0
    
    def has_path():
        from collections import deque
        visited = set()
        queue = deque([(start_row, start_col)])
        visited.add((start_row, start_col))
        
        while queue:
            r, c = queue.popleft()
            if r == end_row and c == end_col:
                return True
            
            for dr, dc in [(0, 1), (1, 0), (0, -1), (-1, 0)]:
                nr, nc = r + dr, c + dc
                if 0 < nr < size - 1 and 0 < nc < size - 1 and (nr, nc) not in visited and maze[nr][nc] == 0:
                    visited.add((nr, nc))
                    queue.append((nr, nc))
        
        return False
    
    if not has_path():
        maze[end_row - 1][end_col] = 0
        maze[end_row][end_col - 1] = 0
        maze[end_row - 2][end_col] = 0
        maze[end_row][end_col - 2] = 0
    
    return maze

def get_current_player():
    if not game_state["player_order"]:
        return None
    return game_state["player_order"][game_state["current_turn_index"]]

def is_valid_move(from_pos, to_pos):
    if not from_pos or not to_pos:
        return False
    
    from_row, from_col = from_pos
    to_row, to_col = to_pos
    maze = game_state["maze"]
    
    if from_row != to_row and from_col != to_col:
        return False
    
    if not (0 <= to_row < len(maze) and 0 <= to_col < len(maze[0])):
        return False
    
    if from_row == to_row:
        start_col = min(from_col, to_col)
        end_col = max(from_col, to_col)
        for col in range(start_col, end_col + 1):
            if maze[from_row][col] == 1:
                return False
    else:
        start_row = min(from_row, to_row)
        end_row = max(from_row, to_row)
        for row in range(start_row, end_row + 1):
            if maze[row][from_col] == 1:
                return False
    
    return True

def broadcast_game_state():
    current_player = get_current_player()
    
    player_points = {}
    for name in game_state["player_order"]:
        player_points[name] = game_state["players"][name].get('points', 0)
    
    state = {
        "is_running": game_state["is_running"],
        "game_mode": game_state["game_mode"],
        "players": game_state["player_order"],
        "player_colors": {name: game_state["players"][name] for name in game_state["player_order"]},
        "player_points": player_points,
        "maze": game_state["maze"],
        "current_position": game_state["current_position"],
        "player_positions": game_state["player_positions"],
        "start_position": game_state["start_position"],
        "end_position": game_state["end_position"],
        "current_player": current_player,
        "moves_remaining": game_state["moves_remaining"],
        "player_moves": game_state["player_moves"],
        "winner": game_state["winner"],
        "finished_players": game_state["finished_players"]
    }
    
    socketio.emit('game_update', state, room=GAME_ROOM)

def next_turn():
    game_state["current_turn_index"] = (game_state["current_turn_index"] + 1) % len(game_state["player_order"])
    game_state["moves_remaining"] = game_state["moves_per_turn"]
    broadcast_game_state()

def end_game(winner_name):
    game_state["winner"] = winner_name
    game_state["is_running"] = False
    
    if winner_name and winner_name in game_state["players"]:
        game_state["players"][winner_name]['points'] += 500
    
    with app.app_context():
        for player_name in game_state["player_order"]:
            player_record = GameHistory.query.filter_by(player_name=player_name).first()
            if not player_record:
                player_record = GameHistory(player_name=player_name, games_played=0, games_won=0, total_points=0)
                db.session.add(player_record)
            
            player_record.games_played += 1
            player_record.total_points += game_state["players"][player_name].get('points', 0)
            
            if player_name == winner_name:
                player_record.games_won += 1
        
        db.session.commit()
    
    broadcast_game_state()
    socketio.emit('game_over', {
        'winner': winner_name, 
        'final_scores': {name: game_state["players"][name].get('points', 0) for name in game_state["player_order"]}
    }, room=GAME_ROOM)
    
    socketio.sleep(5)
    
    game_state["players"].clear()
    game_state["player_order"].clear()
    game_state["player_positions"].clear()
    game_state["player_moves"].clear()
    game_state["finished_players"].clear()
    sid_to_name.clear()
    game_state["is_running"] = False
    game_state["maze"] = None
    game_state["current_position"] = None
    game_state["start_position"] = None
    game_state["end_position"] = None
    game_state["current_turn_index"] = 0
    game_state["moves_remaining"] = 0
    game_state["winner"] = None
    
    broadcast_game_state()

@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")
    
    if request.sid in sid_to_name:
        print(f"Duplicate connection detected for {request.sid}, ignoring")
        return
    
    join_room(GAME_ROOM)
    
    is_host = not request.referrer or '/player' not in request.referrer
    if is_host and not game_state.get("host_sid"):
        join_room(HOST_ROOM)
        game_state["host_sid"] = request.sid
        print(f"Host connected: {request.sid}")
    else:
        join_room(PLAYER_ROOM)
        print(f"Player connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    name = sid_to_name.get(request.sid)
    if name and name in game_state["players"]:
        game_state["players"][name]['connected'] = False
        print(f"Player {name} disconnected")
        if request.sid in sid_to_name:
            del sid_to_name[request.sid]
        broadcast_game_state()
    
    if request.sid == game_state["host_sid"]:
        print("Host disconnected")
        game_state["host_sid"] = None

@socketio.on('join_game')
def handle_join(data):
    name = data.get('name', '').strip()
    if not name:
        emit('join_error', {'message': 'Please enter a name'})
        return
    
    if name in game_state["players"] and game_state["players"][name].get('connected'):
        emit('join_error', {'message': 'This name is already in use'})
        return
    
    if name in game_state["players"]:
        game_state["players"][name]['connected'] = True
        game_state["players"][name]['sid'] = request.sid
    else:
        color_index = len(game_state["player_order"]) % len(PLAYER_COLORS)
        color_data = PLAYER_COLORS[color_index]
        
        game_state["players"][name] = {
            'color': color_data['color'],
            'color_name': color_data['name'],
            'order': len(game_state["player_order"]),
            'sid': request.sid,
            'connected': True,
            'points': 1000
        }
        game_state["player_order"].append(name)
    
    sid_to_name[request.sid] = name
    emit('join_success', {'name': name, 'color': game_state["players"][name]['color']})
    print(f"Player {name} joined")
    
    broadcast_game_state()

@socketio.on('start_game')
def handle_start_game(settings):
    if request.sid != game_state["host_sid"]:
        return
    
    if game_state["is_running"]:
        emit('game_error', {'message': 'Game already running'})
        return
    
    if len(game_state["player_order"]) == 0:
        emit('game_error', {'message': 'No players joined'})
        return
    
    moves_per_turn = int(settings.get('moves_per_turn', 5))
    maze_size = int(settings.get('maze_size', 20))
    game_mode = settings.get('game_mode', 'turn_based')
    
    for player_name in game_state["player_order"]:
        game_state["players"][player_name]['points'] = 1000
    
    maze = generate_maze(maze_size)
    
    game_state["maze"] = maze
    game_state["maze_size"] = maze_size
    game_state["game_mode"] = game_mode
    game_state["start_position"] = [1, 1]
    game_state["end_position"] = [maze_size - 2, maze_size - 2]
    game_state["moves_per_turn"] = moves_per_turn
    game_state["current_turn_index"] = 0
    game_state["is_running"] = True
    game_state["winner"] = None
    game_state["finished_players"] = []
    
    if game_mode == "race":
        for player_name in game_state["player_order"]:
            game_state["player_positions"][player_name] = [1, 1]
            game_state["player_moves"][player_name] = moves_per_turn
    else:
        game_state["current_position"] = [1, 1]
        game_state["moves_remaining"] = moves_per_turn
    
    print(f"Game started with {len(game_state['player_order'])} players in {game_mode} mode")
    
    socketio.emit('game_started', room=GAME_ROOM)
    broadcast_game_state()

@socketio.on('make_move')
def handle_make_move(data):
    player_name = sid_to_name.get(request.sid)
    
    if not player_name or not game_state["is_running"]:
        return
    
    if player_name in game_state["finished_players"]:
        emit('move_error', {'message': 'You have finished or quit'})
        return
    
    game_mode = game_state["game_mode"]
    
    if game_mode == "turn_based":
        current_player = get_current_player()
        if player_name != current_player:
            emit('move_error', {'message': 'Not your turn'})
            return
        
        if game_state["moves_remaining"] <= 0:
            return
        
        to_pos = data.get('position')
        from_pos = game_state["current_position"]
        
        if not is_valid_move(from_pos, to_pos):
            game_state["moves_remaining"] -= 1
            game_state["players"][player_name]['points'] -= 10
            emit('move_result', {'valid': False, 'message': 'Invalid move - wall in the way!'})
            broadcast_game_state()
        else:
            game_state["current_position"] = to_pos
            game_state["moves_remaining"] -= 1
            game_state["players"][player_name]['points'] -= 10
            emit('move_result', {'valid': True, 'message': 'Valid move!'})
            
            end_row = game_state["end_position"][0]
            end_col = game_state["end_position"][1]
            
            if to_pos[0] == end_row and to_pos[1] == end_col:
                print(f"🏆 WINNER! {player_name} reached the end!")
                broadcast_game_state()
                socketio.sleep(0.5)
                end_game(player_name)
                return
            
            broadcast_game_state()
        
        if game_state["moves_remaining"] <= 0:
            player_points = game_state["players"][player_name]['points']
            max_moves = player_points // 50
            if max_moves > 0:
                emit('offer_buy_moves', {'points': player_points, 'cost': 50, 'max_moves': max_moves})
            else:
                socketio.sleep(1)
                next_turn()
    
    else:
        if player_name not in game_state["player_moves"] or game_state["player_moves"][player_name] <= 0:
            return
        
        to_pos = data.get('position')
        from_pos = game_state["player_positions"][player_name]
        
        if not is_valid_move(from_pos, to_pos):
            game_state["player_moves"][player_name] -= 1
            game_state["players"][player_name]['points'] -= 10
            emit('move_result', {'valid': False, 'message': 'Invalid move - wall in the way!'})
            broadcast_game_state()
        else:
            game_state["player_positions"][player_name] = to_pos
            game_state["player_moves"][player_name] -= 1
            game_state["players"][player_name]['points'] -= 10
            emit('move_result', {'valid': True, 'message': 'Valid move!'})
            
            end_row = game_state["end_position"][0]
            end_col = game_state["end_position"][1]
            
            if to_pos[0] == end_row and to_pos[1] == end_col:
                print(f"🏆 WINNER! {player_name} reached the end!")
                game_state["finished_players"].append(player_name)
                broadcast_game_state()
                socketio.sleep(0.5)
                end_game(player_name)
                return
            
            broadcast_game_state()
        
        if game_state["player_moves"][player_name] <= 0:
            player_points = game_state["players"][player_name]['points']
            max_moves = player_points // 50
            if max_moves > 0:
                emit('offer_buy_moves', {'points': player_points, 'cost': 50, 'max_moves': max_moves})
            else:
                emit('offer_quit', {'message': 'Out of moves and points!'})

@socketio.on('buy_moves')
def handle_buy_moves(data):
    player_name = sid_to_name.get(request.sid)
    
    if not player_name or not game_state["is_running"]:
        return
    
    buy = data.get('buy', False)
    num_moves = int(data.get('num_moves', 1))
    game_mode = game_state["game_mode"]
    
    if buy and num_moves > 0:
        player = game_state["players"][player_name]
        total_cost = num_moves * 50
        
        if player['points'] >= total_cost:
            player['points'] -= total_cost
            
            if game_mode == "turn_based":
                current_player = get_current_player()
                if player_name != current_player:
                    return
                game_state["moves_remaining"] = num_moves
            else:
                game_state["player_moves"][player_name] = num_moves
            
            print(f"{player_name} bought {num_moves} extra moves for {total_cost} points")
            broadcast_game_state()
            emit('buy_success', {'message': f'You bought {num_moves} extra moves!'})
        else:
            emit('buy_error', {'message': 'Not enough points!'})
            if game_mode == "turn_based":
                socketio.sleep(1)
                next_turn()
            else:
                emit('offer_quit', {'message': 'Out of moves and points!'})
    else:
        if game_mode == "turn_based":
            socketio.sleep(1)
            next_turn()

@socketio.on('quit_game')
def handle_quit_game():
    player_name = sid_to_name.get(request.sid)
    
    if not player_name or not game_state["is_running"]:
        return
    
    if game_state["game_mode"] == "race":
        game_state["finished_players"].append(player_name)
        print(f"{player_name} quit the game")
        emit('quit_success', {'message': 'You have quit the game'})
        broadcast_game_state()

@socketio.on('force_end_game')
def handle_force_end_game():
    if request.sid == game_state["host_sid"]:
        print("Host is forcing game to end")
        
        game_state["is_running"] = False
        socketio.emit('game_over', {'winner': None, 'forced': True}, room=GAME_ROOM)
        
        socketio.sleep(2)
        
        game_state["players"].clear()
        game_state["player_order"].clear()
        game_state["player_positions"].clear()
        game_state["player_moves"].clear()
        game_state["finished_players"].clear()
        sid_to_name.clear()
        game_state["maze"] = None
        game_state["current_position"] = None
        game_state["start_position"] = None
        game_state["end_position"] = None
        game_state["current_turn_index"] = 0
        game_state["moves_remaining"] = 0
        game_state["winner"] = None
        
        broadcast_game_state()

if __name__ == '__main__':
    game_state["players"].clear()
    game_state["player_order"].clear()
    sid_to_name.clear()
    
    with app.app_context():
        db.create_all()
        print("Database initialized")
    
    port = int(os.environ.get('PORT', 5000))
    print("=" * 50)
    print(f"Starting Maze Game Server on port {port}")
    print("All game state cleared - starting fresh")
    print("=" * 50)
    socketio.run(app, debug=False, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)