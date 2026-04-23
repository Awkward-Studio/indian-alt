import os
import json
import re
from pathlib import Path
from collections import defaultdict

def safe_cleanup():
    base_dir = Path("data/extractions")
    if not base_dir.exists():
        print(f"Error: {base_dir} not found.")
        return

    # Regex to capture: 1=BaseName, 2=StartPage, 3=EndPage, 4=Extension
    chunk_pattern = re.compile(r"(.+) \[Pages (\d+)-(\d+)\](\.[^.]+)\.json$")
    
    print(f"\n>>> STARTING SAFETY-FIRST CLEANUP")
    print("-" * 60)

    for deal_dir in base_dir.iterdir():
        if not deal_dir.is_dir(): continue
        
        # Group chunks by their parent document
        # Map: BaseFilename.ext -> [list of chunk files]
        document_groups = defaultdict(list)
        
        for file in deal_dir.glob("*.json"):
            match = chunk_pattern.match(file.name)
            if match:
                base_name = match.group(1)
                ext = match.group(4)
                parent_name = f"{base_name}{ext}"
                document_groups[parent_name].append({
                    'path': file,
                    'start': int(match.group(2)),
                    'name': file.name
                })

        if not document_groups: continue

        print(f"\nDeal: {deal_dir.name}")
        
        for parent_name, chunks in document_groups.items():
            merged_path = deal_dir / f"{parent_name}.json"
            chunks.sort(key=lambda x: x['start']) # Ensure correct page order
            
            if merged_path.exists():
                print(f"  [OK] {parent_name}: Merged file exists. Deleting {len(chunks)} chunks...")
                for c in chunks:
                    os.remove(c['path'])
            else:
                print(f"  [FIXING] {parent_name}: Merged file MISSING. Reconstructing from {len(chunks)} chunks...")
                try:
                    all_text = []
                    for c in chunks:
                        with open(c['path'], 'r') as f:
                            data = json.load(f)
                            all_text.append(data.get("normalized_text") or data.get("text") or "")
                    
                    # Create the final merged file
                    final_data = {
                        "normalized_text": "\n\n".join(all_text),
                        "raw_extracted_text": "\n\n".join(all_text),
                        "transcription_status": "complete",
                        "quality_flags": ["reconstructed_from_chunks"]
                    }
                    with open(merged_path, 'w') as f:
                        json.dump(final_data, f, indent=2)
                    
                    print(f"  [SUCCESS] Created {merged_path.name}. Now cleaning up...")
                    for c in chunks:
                        os.remove(c['path'])
                except Exception as e:
                    print(f"  [ERROR] Failed to reconstruct {parent_name}: {e}")

    print("-" * 60)
    print("Cleanup and Recovery complete.")

if __name__ == "__main__":
    safe_cleanup()
