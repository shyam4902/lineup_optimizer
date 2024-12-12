from flask import Flask, request, render_template, redirect, url_for
from werkzeug.utils import secure_filename
import os
import pandas as pd

app = Flask(__name__)


# Define contest configurations
CONTESTS = {
    "Spark": {"lineup": {"QB": 1, "RB": 1, "WR": 1, "TE": 1}, "salary_cap": 32000},
    "Scorcher": {"lineup": {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "Flex": 1}, "salary_cap": 36750},
    "Wildfire": {"lineup": {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "Flex": 2}, "salary_cap": 48000},
    "Inferno": {"lineup": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "Flex": 1, "Superflex": 1}, "salary_cap": 64000},
}

# Configure upload folder and allowed extensions
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'csv'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure the upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def home():
    return '''
        <!doctype html>
        <title>Lineup Optimizer</title>
        <h1>Upload your Roster</h1>
        <form action="/upload" method="post" enctype="multipart/form-data">
            <input type="file" name="file">
            <input type="submit" value="Upload">
        </form>
    '''

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return "No file part"
    file = request.files['file']
    if file.filename == '':
        return "No selected file"
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        return redirect(url_for('process_file', filename=filename))
    return "File not allowed"

@app.route('/process/<filename>')
def process_file(filename):
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    try:
        # Load the CSV
        df = pd.read_csv(filepath)

        # Store the processed data temporarily
        df.to_csv(os.path.join(app.config['UPLOAD_FOLDER'], "processed_roster.csv"), index=False)

        # Redirect to lineup selection
        return '''
            <h1>File Processed Successfully</h1>
            <a href="/lineups">Generate Lineups</a>
        '''

    except Exception as e:
        return f"Error processing file: {e}"


from itertools import combinations

@app.route('/lineups', methods=['GET', 'POST'])
def lineups():
    # Load the processed roster
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], "processed_roster.csv")
    df = pd.read_csv(filepath)

    if request.method == 'POST':
        contest_type = request.form.get("contest")
        contest_settings = CONTESTS.get(contest_type)

        if not contest_settings:
            return f"Error: Contest type '{contest_type}' not found."

        lineup_requirements = contest_settings["lineup"]
        salary_cap = contest_settings["salary_cap"]

        # Initialize results
        lineups = []
        available_players = df.copy()

        while True:
            # Generate all valid combinations for the lineup
            lineup = []
            total_salary = 0
            total_projection = 0

            # Ensure all positions are filled
            try:
                for position, count in lineup_requirements.items():
                    if position in ["Flex", "Superflex"]:
                        valid_positions = ["RB", "WR", "TE"] if position == "Flex" else ["QB", "RB", "WR", "TE"]
                    else:
                        valid_positions = [position]

                    # Get candidates for the position
                    candidates = available_players[available_players["position"].isin(valid_positions)]

                    # Ensure enough players are available for the position
                    if len(candidates) < count:
                        raise ValueError(f"Not enough players to fill {count} {position} slots.")

                    # Select top-scoring players for the position
                    selected_players = candidates.nlargest(count, "GB_Projection")
                    for _, player in selected_players.iterrows():
                        lineup.append(player)
                        total_salary += player["salary"]
                        total_projection += player["GB_Projection"]

                        # Remove used players
                        available_players = available_players[available_players["Player"] != player["Player"]]

                # Check lineup's total salary
                if total_salary > salary_cap:
                    raise ValueError("Salary cap exceeded.")

                # Append the lineup details
                lineups.append({
                    "lineup": pd.DataFrame(lineup),
                    "total_salary": total_salary,
                    "salary_remaining": salary_cap - total_salary,
                    "total_projection": total_projection
                })

            except ValueError as e:
                # If a lineup cannot be completed, stop
                break

            # Stop if there are not enough players left for another lineup
            if len(available_players) < sum(lineup_requirements.values()):
                break

        # Display the generated lineups
        result = ""
        for i, lineup_info in enumerate(lineups, 1):
            lineup = pd.DataFrame(lineup_info["lineup"])
            result += f"<h2>Lineup {i}</h2>{lineup.to_html(index=False)}"
            result += f"""
                <p><strong>Total Salary:</strong> {lineup_info['total_salary']}</p>
                <p><strong>Salary Remaining:</strong> {lineup_info['salary_remaining']}</p>
                <p><strong>Total Projected Points:</strong> {lineup_info['total_projection']:.2f}</p>
                <hr>
            """

        return result

    return '''
        <!doctype html>
        <title>Generate Lineups</title>
        <h1>Select Contest Type</h1>
        <form method="post">
            <label for="contest">Choose a Contest:</label>
            <select name="contest" id="contest">
                <option value="Spark">Spark</option>
                <option value="Scorcher">Scorcher</option>
                <option value="Wildfire">Wildfire</option>
                <option value="Inferno">Inferno</option>
            </select>
            <input type="submit" value="Generate Lineups">
        </form>
    '''


if __name__ == '__main__':
    app.run(debug=True)
