## Amazon Beauty Bestseller Tracker - Project Setup Checklist

### ✅ Verify Project Structure
- Python project for scraping and filtering Amazon bestsellers
- Location: `/Users/undomiel/workspace/amazon-bestseller-tracker`
- Required files created:
  - `main.py` - Main application
  - `requirements.txt` - Python dependencies
  - `config.json` - Configuration file
  - `README.md` - Documentation

### ✅ Install Dependencies
```bash
pip install -r requirements.txt
```

### ✅ Project Features
- Fetch Amazon beauty bestsellers from specified URL
- Store data in JSON format
- Filter results by keyword
- Auto-update every 6 hours using APScheduler
- Interactive command-line interface

### ✅ Usage
```bash
python main.py
```

Available commands:
- `update` - Update data now
- `show` - Display all bestsellers
- `filter <keyword>` - Filter by keyword
- `auto` - Start auto-update
- `exit` - Exit program

### ✅ Configuration
Edit `config.json` to customize:
- Amazon URL
- Update interval (hours)
- Data file location
- User-Agent header
