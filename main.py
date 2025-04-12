import pandas as pd
from ollama import chat, ChatResponse
import logging, re, json, time 
from typing import Dict, List, Tuple
from fuzzywuzzy import fuzz

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class MaterialTextProcessor:
    def __init__(self, excel_path: str):
        self.df = pd.read_excel(excel_path)
        self.answer_list = []
        self.llm_calls = 0
        self.llm_errors = 0
        self.llm_fallbacks = 0
    
    def _preprocess_text(self, text: str) -> str:
        """Clean and standardize input text"""
        if not isinstance(text, str):
            return ""
        
        # Replace multiple spaces with single space
        text = ' '.join(text.split())
        
        # Handle common patterns
        text = text.replace("//", " | ")  # Replace double slashes with pipe separator
        text = re.sub(r'\s+\|\s+', ' | ', text)  # Normalize separators
        text = re.sub(r'\s+', ' ', text)  # Remove extra spaces
        
        return text.strip()
    
    def _create_llm_prompt(self, text: str) -> str:
        """Create the LLM prompt with simplified instructions"""
        return f"""Extract product information from this German material description in JSON format:
{{
    "product_name": "main product name",
    "characteristics": ["key specs"],
    "material_type": "type",
    "unit_of_measure": "ST",
    "categorization": {{}},
    "short_description": "concise description under 40 chars"
}}

Description: {text}

Rules:
1. Keep short_description under 40 characters
2. Include key distinguishing features
3. For items starting with "für", include what it's for in the name
4. Preserve part numbers and specifications
5. For technical components, include type/size/material

Example:
Input: "für APIC Wasserfilter FMA 9000 | Ref: 9000/CPF01/230/VH | Pos. 48"
Output: {{
    "product_name": "APIC Wasserfilter FMA 9000",
    "characteristics": ["Ref: 9000/CPF01/230/VH", "Pos. 48"],
    "material_type": "filter",
    "unit_of_measure": "ST",
    "categorization": {{}},
    "short_description": "APIC Wasserfilter FMA 9000"
}}

Input: "Siemens Schütz | Spulensp. 230V, 50HZ/AC | Leistung 45,0 KW/400V"
Output: {{
    "product_name": "Siemens Schütz",
    "characteristics": ["230V, 50HZ/AC", "45,0 KW/400V"],
    "material_type": "electrical",
    "unit_of_measure": "ST",
    "categorization": {{
        "Spannung (V)": "230",
        "Leistung (kW)": "45.0"
    }},
    "short_description": "Siemens Schütz 230V 45KW"
}}
"""
    
    def _parse_llm_response(self, content: str) -> Dict:
        """Parse the LLM response with better error handling"""
        try:
            # Clean the response
            content = content.strip()
            
            # Find JSON portion
            start = content.find('{')
            end = content.rfind('}') + 1
            
            if start < 0 or end <= 0:
                raise ValueError("No JSON found in response")
            
            json_str = content[start:end]
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            logger.error(f"Problematic content: {content}")
            raise
        except Exception as e:
            logger.error(f"Response parsing error: {e}")
            raise
    
    def _simple_extraction(self, text: str) -> Dict:
        """Try to extract information without LLM"""
        try:
            parts = [p.strip() for p in text.split('|') if p.strip()]
            
            if not parts:
                return self._create_fallback_info(text)
            
            # Handle "für X" patterns
            if parts[0].lower().startswith('für '):
                product_name = parts[0][4:].strip()
            else:
                product_name = parts[0]
            
            characteristics = parts[1:] if len(parts) > 1 else []
            
            # Determine material type based on keywords
            material_type = "other"
            type_keywords = {
                "filter": ["filter", "wasserfilter"],
                "electrical": ["schütz", "relais", "spannung", "leistung"],
                "mechanical": ["lager", "welle", "ring", "buchse"],
                "seal": ["dicht", "dichtung"]
            }
            
            for type_name, keywords in type_keywords.items():
                if any(kw in text.lower() for kw in keywords):
                    material_type = type_name
                    break
            
            # Create short description
            short_desc = product_name
            if characteristics:
                # Add first characteristic if it fits
                if len(short_desc) + 1 + len(characteristics[0]) <= 40:
                    short_desc += " " + characteristics[0]
                else:
                    # Try to add part of first characteristic
                    remaining_space = 40 - len(short_desc) - 1
                    if remaining_space > 3:  # Need at least 3 chars to be useful
                        short_desc += " " + characteristics[0][:remaining_space]
            
            # Ensure short description is within limits
            short_desc = short_desc[:40]
            
            return {
                "product_name": product_name,
                "characteristics": characteristics,
                "material_type": material_type,
                "unit_of_measure": "ST",
                "categorization": {},
                "short_description": short_desc
            }
            
        except Exception as e:
            logger.error(f"Simple extraction error: {e}")
            return self._create_fallback_info(text)
    
    def _call_llm_for_extraction(self, text: str) -> Dict:
        """Handle the LLM call with retries and better error handling"""
        max_retries = 3
        retry_delay = 2  # seconds
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Calling LLM (attempt {attempt + 1}/{max_retries}) for text: {text[:50]}...")
                start_time = time.time()
                
                response = chat(
                    model='gemma3:latest',
                    messages=[{
                        'role': 'user',
                        'content': self._create_llm_prompt(text)
                    }]
                )
                
                if not response or not response.message:
                    raise ValueError("Empty LLM response")
                
                self.llm_calls += 1
                logger.info(f"LLM call completed in {time.time() - start_time:.2f}s")
                
                return self._parse_llm_response(response.message.content)
                
            except ConnectionError as e:
                logger.warning(f"Connection error (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (attempt + 1))
                continue
                
            except Exception as e:
                logger.error(f"LLM error (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                continue
        
        # If we get here, all retries failed
        self.llm_errors += 1
        raise RuntimeError("All LLM attempts failed")
    
    def _extract_with_fallback(self, text: str) -> Dict:
        """Extraction with fallback to simple method if LLM fails"""
        try:
            # First try simple extraction
            result = self._simple_extraction(text)
            
            # If simple extraction looks too basic, try LLM
            if (len(result["characteristics"]) == 0 or 
                len(result["product_name"].split()) == 1):
                result = self._call_llm_for_extraction(text)
                
            return result
            
        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            self.llm_fallbacks += 1
            return self._create_fallback_info(text)
    
    def _create_fallback_info(self, text: str) -> Dict:
        """Create a more intelligent fallback"""
        parts = [p.strip() for p in text.split('|') if p.strip()]
        
        if not parts:
            return {
                "product_name": "",
                "characteristics": [],
                "material_type": "other",
                "unit_of_measure": "ST",
                "categorization": {},
                "short_description": ""
            }
        
        # Take first part as name
        name = parts[0]
        if name.lower().startswith("für "):
            name = name[4:].strip()
        
        # Create short description - try to include first characteristic if possible
        short_desc = name
        if len(parts) > 1 and len(name) + len(parts[1]) + 1 <= 40:
            short_desc = f"{name} {parts[1]}"
        short_desc = short_desc[:40]
        
        return {
            "product_name": name,
            "characteristics": parts[1:] if len(parts) > 1 else [],
            "material_type": "other",
            "unit_of_measure": "ST",
            "categorization": {},
            "short_description": short_desc
        }
    
    def _create_standardized_description(self, info: Dict) -> str:
        """Create final standardized description"""
        # Use short_description if available and valid
        if info.get("short_description"):
            desc = info["short_description"]
            if len(desc) <= 40:
                return desc
        
        # Fallback construction
        desc = info["product_name"]
        if info["characteristics"]:
            # Add first characteristic if it fits
            char = info["characteristics"][0]
            if len(desc) + 1 + len(char) <= 40:
                desc += " " + char
            else:
                # Add abbreviated characteristic
                space_left = 40 - len(desc) - 1
                if space_left >= 3:  # Need at least 3 chars to be useful
                    desc += " " + char[:space_left]
        
        return desc[:40]
    
    def process_materials(self):
        """Process all material texts"""
        logger.info(f"Starting to process {len(self.df)} materials")
        start_time = time.time()
        
        for idx, row in self.df.iterrows():
            try:
                # Get text from appropriate columns
                base_text = str(row.iloc[2]) if len(row) > 2 else ""
                text = str(row.iloc[3]) if len(row) > 3 else ""
                
                # Preprocess text
                cleaned_text = self._preprocess_text(text)
                if not cleaned_text:
                    logger.warning(f"Empty text at index {idx}")
                    continue
                
                # Extract structured information
                product_info = self._extract_with_fallback(cleaned_text)
                
                # Create standardized description
                final_description = self._create_standardized_description(product_info)
                
                # Store results
                self.answer_list.append({
                    "original_text": text,
                    "cleaned_text": cleaned_text,
                    "structured_info": product_info,
                    "final_description": final_description
                })
                
                # Print results to console
                print("\n" + "="*80)
                print(f"Entry {idx + 1}/{len(self.df)}")
                print("-"*80)
                print(f"Original: {text}")
                print(f"Processed: {final_description}")
                print(f"Structured Info: {json.dumps(product_info, indent=2, ensure_ascii=False)}")
                print("="*80 + "\n")
                
                logger.info(f"Processed entry {idx + 1}/{len(self.df)}")
                
            except Exception as e:
                logger.error(f"Error processing entry {idx}: {e}")
                # Store error information
                self.answer_list.append({
                    "original_text": text,
                    "cleaned_text": cleaned_text if 'cleaned_text' in locals() else "",
                    "error": str(e),
                    "final_description": "ERROR"
                })
        
        end_time = time.time()
        logger.info(f"Processing completed in {end_time - start_time:.2f} seconds")
        logger.info(f"Total LLM calls: {self.llm_calls}")
        logger.info(f"LLM errors: {self.llm_errors}")
        logger.info(f"Fallbacks to simple extraction: {self.llm_fallbacks}")
    
    def save_results(self, output_path: str):
        """Save processed results to Excel"""
        results_df = pd.DataFrame(self.answer_list)
        results_df.to_excel(output_path, index=False)
        logger.info(f"Results saved to {output_path}")

def main():
    processor = MaterialTextProcessor("SAP_ERSA_Materialtexte_Südstärke.xlsx")
    processor.process_materials()
    processor.save_results("processed_materials.xlsx")

if __name__ == "__main__":
    main()