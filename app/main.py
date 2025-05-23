"""
Main application file for the Legal Sanctions RAG system.
"""

from flask import Flask, render_template, request, jsonify, session, after_this_request, g, redirect, url_for, Response, stream_with_context
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, create_refresh_token, jwt_required, get_jwt_identity, verify_jwt_in_request
from functools import wraps
import os
import chromadb
from chromadb.config import Settings
import uuid
import werkzeug
import time
import re
import json
from werkzeug.utils import secure_filename
from langchain_community.embeddings import HuggingFaceEmbeddings
from transformers import pipeline
import torch
from unstructured.partition.pdf import partition_pdf
import dotenv
import shutil
from datetime import datetime
import nltk
from app.models.chat import ChatStorage
import PyPDF2
import io
import tempfile
from PIL import Image
import secrets
try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False

# Load environment variables
dotenv.load_dotenv()

# Download necessary NLTK data
try:
    nltk.download('punkt', quiet=True)
    nltk.download('averaged_perceptron_tagger', quiet=True)
    # For English only
    nltk.download('maxent_ne_chunker', quiet=True)
    nltk.download('words', quiet=True)
except Exception as e:
    print(f"Warning: NLTK download error: {str(e)}")

# Local imports
from app.config import (
    CHROMA_DIR, 
    DOCUMENTS_DIR, 
    EMBEDDING_MODEL,
    DEEPSEEK_API_KEY, 
    DEEPSEEK_API_BASE,
    OPENROUTER_API_KEY,
    OPENROUTER_API_BASE,
    MODEL_PROVIDER,
    MODEL_NAME,
    SECRET_KEY,
    DEFAULT_DATASETS,
    ROLE_PERMISSIONS
)
from app.utils.deepseek_client import DeepSeekClient
from app.utils.openrouter_client import OpenRouterClient

app = Flask(__name__)
app.secret_key = SECRET_KEY
# Configure session to be permanent and last for 31 days
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * 31  # 31 days in seconds
CORS(app)

# Initialize Chroma
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)

# Initialize embeddings
embeddings = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)

# Initialize the OpenRouter client with the new DeepSeek model
print(f"Using OpenRouter with model: deepseek/deepseek-chat-v3-0324:free")
llm_client = OpenRouterClient(
    api_key=OPENROUTER_API_KEY, 
    api_base=OPENROUTER_API_BASE,
    model="deepseek/deepseek-chat-v3-0324:free"
)

# Check for GPU
device = "cuda" if torch.cuda.is_available() else "cpu"

# Initialize JWT manager for authentication
jwt = JWTManager(app)
app.config['JWT_SECRET_KEY'] = os.environ.get("JWT_SECRET_KEY", SECRET_KEY)
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = int(os.environ.get("JWT_ACCESS_TOKEN_EXPIRES", 60 * 60))  # 1 hour
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = int(os.environ.get("JWT_REFRESH_TOKEN_EXPIRES", 60 * 60 * 24 * 30))  # 30 days

# Import and initialize additional components
from app.models.user import User
from app.utils.audit_logger import AuditLogger
from app.utils.feedback import FeedbackManager
from app.utils.credit_system import CreditSystem
from app.utils.encryption import DocumentEncryption
from app.utils.secure_processor import SecureDocumentProcessor

# Initialize user management
user_manager = User()

# Initialize audit logger
from app.config import ENABLE_AUDIT_LOGGING, MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD
from app.config import MAIL_DEFAULT_SENDER, MAIL_FEEDBACK_RECIPIENT, MAIL_USE_TLS, DOCUMENT_ENCRYPTION_KEY

# Create audit logger
audit_logger = AuditLogger(enabled=os.environ.get("ENABLE_AUDIT_LOGGING", "True").lower() == "true")

# Initialize feedback manager
feedback_manager = FeedbackManager(
    smtp_server=os.environ.get("MAIL_SERVER", MAIL_SERVER),
    smtp_port=int(os.environ.get("MAIL_PORT", MAIL_PORT)),
    smtp_username=os.environ.get("MAIL_USERNAME", MAIL_USERNAME),
    smtp_password=os.environ.get("MAIL_PASSWORD", MAIL_PASSWORD),
    sender_email=os.environ.get("MAIL_DEFAULT_SENDER", MAIL_DEFAULT_SENDER),
    recipient_email=os.environ.get("MAIL_FEEDBACK_RECIPIENT", MAIL_FEEDBACK_RECIPIENT),
    use_tls=os.environ.get("MAIL_USE_TLS", str(MAIL_USE_TLS)).lower() == "true"
)

# Initialize credit system
credit_system = CreditSystem()

# Initialize secure document components
# Use the Fernet key generation if needed
if not os.environ.get("DOCUMENT_ENCRYPTION_KEY"):
    # Make sure we use the fixed DOCUMENT_ENCRYPTION_KEY from config.py
    print("Using auto-generated encryption key")
else:
    print("Using environment variable for encryption key")

document_encryption = DocumentEncryption(key=os.environ.get("DOCUMENT_ENCRYPTION_KEY", DOCUMENT_ENCRYPTION_KEY))
secure_processor = SecureDocumentProcessor(
    encryption_handler=document_encryption,
    embedding_model=EMBEDDING_MODEL,
    chroma_path=CHROMA_DIR,
    device=device
)

# Initialize chat storage
chat_storage = ChatStorage()

# Define base directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_METADATA_FILE = os.path.join(BASE_DIR, "data", "dataset_metadata.json")

# Helper functions for dataset metadata
def load_dataset_metadata():
    if not os.path.exists(DATASET_METADATA_FILE):
        return {}
    try:
        with open(DATASET_METADATA_FILE, 'r') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}

def save_dataset_metadata(metadata):
    try:
        with open(DATASET_METADATA_FILE, 'w') as f:
            json.dump(metadata, f, indent=2)
    except IOError:
        print(f"Error: Could not save dataset metadata to {DATASET_METADATA_FILE}")

# Configure file uploads
UPLOAD_FOLDER = os.path.join(BASE_DIR, "data", "uploads")
ALLOWED_EXTENSIONS = {'pdf', 'txt'}
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB

# Create upload folder if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Configure Flask app
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

def allowed_file(filename):
    """Check if the file has an allowed extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def robust_extract_text_from_pdf(pdf_path):
    """Extract text from PDF using multiple methods for better reliability."""
    # Store all extracted text
    all_text = []
    extraction_success = False
    
    # METHOD 1: Using unstructured library (works well for many PDFs)
    try:
        print(f"Trying unstructured partition for {pdf_path}")
        # Try without extract_images_in_pdf parameter first
        try:
            elements = partition_pdf(pdf_path, strategy="hi_res")
        except TypeError:
            # If that fails, try with older version parameters
            elements = partition_pdf(pdf_path)
            
        for element in elements:
            if hasattr(element, 'text') and element.text:
                all_text.append(element.text)
                extraction_success = True
    except Exception as e:
        print(f"Error with unstructured partition: {str(e)}")
    
    # If first method got text, return it
    if extraction_success and len(''.join(all_text).strip()) > 100:
        print(f"Successfully extracted text using unstructured partition")
        return all_text
    
    # METHOD 2: Using PyPDF2 (good for text-based PDFs)
    try:
        print(f"Trying PyPDF2 for {pdf_path}")
        pdf_text = []
        with open(pdf_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            if len(pdf_reader.pages) > 0:
                for page_num in range(len(pdf_reader.pages)):
                    page = pdf_reader.pages[page_num]
                    text = page.extract_text()
                    if text and text.strip():
                        pdf_text.append(f"Page {page_num+1}: {text}")
                        extraction_success = True
        
        # If we got text, add it to all_text
        if extraction_success:
            all_text.extend(pdf_text)
            print(f"Successfully extracted text using PyPDF2")
    except Exception as e:
        print(f"Error with PyPDF2: {str(e)}")
    
    # METHOD 3: Using OCR if available and needed
    if not extraction_success and PYTESSERACT_AVAILABLE:
        try:
            print(f"Trying OCR for {pdf_path}")
            # Convert PDF to images and OCR
            from pdf2image import convert_from_path
            
            ocr_text = []
            images = convert_from_path(pdf_path)
            for i, image in enumerate(images):
                text = pytesseract.image_to_string(image)
                if text and text.strip():
                    ocr_text.append(f"Page {i+1} (OCR): {text}")
                    extraction_success = True
            
            if extraction_success:
                all_text.extend(ocr_text)
                print(f"Successfully extracted text using OCR")
        except Exception as e:
            print(f"Error with OCR: {str(e)}")
    
    # If no text was extracted, add a message
    if not extraction_success or not all_text:
        print(f"Could not extract any text from {pdf_path}")
        return []
    
    return all_text

@app.route('/')
def index():
    """Render the main chat interface."""
    # Mark the session as permanent so it persists beyond browser close
    session.permanent = True
    
    # Ensure session has a session_id (used for tracking user sessions)
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
    
    # Optionally, you can track last_active_chat here if you want to reopen the last chat automatically
    last_active_chat = session.get('last_active_chat', None)
    
    return render_template('index.html', 
                          datasets=DEFAULT_DATASETS,
                          last_active_chat=last_active_chat)

@app.route('/api/chat', methods=['POST'])
def chat():
    """Process chat messages and return AI responses with relevant context."""
    data = request.json
    user_message = data.get('message', '')
    dataset_name = data.get('dataset', 'EU-Sanctions')
    history = data.get('history', [])
    
    # Get session ID for this conversation
    if 'session_id' not in session:
        session.permanent = True
        session['session_id'] = str(uuid.uuid4())
    
    # Retrieve context from Chroma based on user query
    collection = chroma_client.get_or_create_collection(name=dataset_name)
    
    # Get the document count to determine how many results we can request
    doc_count = collection.count()
    n_results = min(5, max(1, doc_count))  # Request at most 5, but at least 1 if available
    
    # Handle the case of an empty collection
    if doc_count == 0:
        results = {
            'documents': [["No documents found in the selected dataset. Please upload documents or select a different dataset."]],
            'metadatas': [[{"source": "System", "page": 0}]]
        }
    else:
        # Import the document processor for advanced querying
        from app.utils.document_processor import LegalDocumentProcessor
        
        # Initialize document processor with the same settings as configured globally
        doc_processor = LegalDocumentProcessor(
            embedding_model=EMBEDDING_MODEL,
            chroma_path=CHROMA_DIR,
            device=device
        )
        
        # Use advanced query with hybrid search and reranking
        results = doc_processor.query_dataset(
            dataset_name=dataset_name,
            query=user_message,
            n_results=n_results,
            use_hybrid_search=True,
            use_reranking=True
        )
    
    context = '\n\n'.join(results['documents'][0])
    
    # Extract sources information for better citation
    sources = []
    for doc, meta in zip(results['documents'][0], results['metadatas'][0]):
        source_info = {
            "source": meta.get("source", "Unknown"),
            "page": meta.get("page", meta.get("part", 0)),
            "snippet": doc[:150] + "..." if len(doc) > 150 else doc  # Short preview
        }
        sources.append(source_info)
    
    # Create enhanced prompt for DeepSeek with Chain of Thought reasoning
    system_message = f"""You are a legal expert specializing in international sanctions regulations.
    
    # Instructions
    - Use the provided context to analyze the user's question thoroughly
    - Implement chain-of-thought reasoning by breaking down your analysis step by step
    - First carefully examine the relevant sections from the provided context
    - Think about what legal principles apply to this situation
    - Consider multiple perspectives and interpretations if applicable
    - Draw connections between different parts of the context
    - Formulate a comprehensive and legally sound analysis
    - Cite specific articles, sections, or provisions when possible
    - Clearly separate your reasoning process from your final conclusion
    - If you don't know the answer or it's not in the context, state this clearly
    
    # Output Format
    Structure your response with these sections:
    1. SOURCES: A brief bulleted list of the most relevant source documents you're drawing from
    2. ANALYSIS: Your step-by-step reasoning about the question (this should be detailed)
    3. APPLICABLE PROVISIONS: Specific articles, sections, or legal provisions that apply
    4. CONCLUSION: Your final answer based on the analysis
    
    # Context
    {context}
    """
    
    # Format the conversation history
    formatted_history = []
    for msg in history:
        if msg["role"] in ["user", "assistant"]:
            formatted_history.append({"role": msg["role"], "content": msg["content"]})
    
    try:
        # Call LLM API with enhanced prompt
        response = llm_client.generate_with_rag(
            query=user_message,
            context=system_message,  # Pass the complete system message with instructions
            chat_history=formatted_history[-10:] if formatted_history else [],  # Use last 5 turns (10 messages)
            temperature=0.1  # Lower temperature for more analytical responses
        )
    except Exception as e:
        print(f"Error calling LLM API: {str(e)}")
        response = "I'm sorry, I encountered an error processing your request. Please try again later."
        
    # Add source information to the response context
    source_context = "\n\n".join([f"Source: {s['source']} (Page/Section: {s['page']})\nPreview: {s['snippet']}" for s in sources])
    
    return jsonify({
        'response': response,
        'context': source_context,  # Use the formatted source context with citation info
        'dataset': dataset_name,
        'raw_context': context  # Include the raw context as well if needed
    })

@app.route('/api/datasets', methods=['GET'])
def get_datasets():
    """Return available datasets."""
    # List collections in Chroma
    try:
        collections = chroma_client.list_collections()
        collection_names = [c.name for c in collections]
        
        # Debug output
        print(f"Found Chroma collections: {collection_names}")
        
        # Combine with default datasets
        all_datasets = []
        dataset_metadata = load_dataset_metadata()
        
        # Add custom datasets first
        for name in collection_names:
            # Count documents in collection
            try:
                coll = chroma_client.get_collection(name=name)
                doc_count = coll.count()
                metadata = dataset_metadata.get(name, {})
                description = metadata.get("description", f"Custom dataset with {doc_count} entries")
                author = metadata.get("author")
                topic = metadata.get("topic")
                linkedin_url = metadata.get("linkedin_url")
                custom_instructions = metadata.get("custom_instructions")
                last_update_date = metadata.get("last_update_date")
                all_datasets.append({
                    "name": name, 
                    "description": description,
                    "author": author,
                    "topic": topic,
                    "linkedin_url": linkedin_url,
                    "custom_instructions": custom_instructions,
                    "last_update_date": last_update_date,
                    "document_count": doc_count,
                    "is_custom": True
                })
            except Exception as e:
                print(f"Error getting collection info for {name}: {str(e)}")
                description = dataset_metadata.get(name, {}).get("description", "Custom dataset")
                all_datasets.append({
                    "name": name, 
                    "description": description,
                    "is_custom": True
                })
        
        # Then add default datasets that aren't already in the list
        for dataset in DEFAULT_DATASETS:
            if not any(d['name'] == dataset['name'] for d in all_datasets):
                dataset["is_custom"] = False
                all_datasets.append(dataset)
        
        return jsonify(all_datasets)
    except Exception as e:
        print(f"Error fetching datasets: {str(e)}")
        # Fallback to default datasets
        return jsonify([{"name": "Default", "description": "Default dataset (error occurred)", "is_custom": False}])

@app.route('/api/datasets/<dataset_name>', methods=['DELETE'])
def delete_dataset(dataset_name):
    """Delete a dataset by name."""
    if dataset_name in [d['name'] for d in DEFAULT_DATASETS]:
        return jsonify({"error": "Cannot delete default dataset"}), 400
    
    deleted_from_chroma = False
    deletion_messages = []
    
    try:
        # Attempt to delete from Chroma
        chroma_client.delete_collection(name=dataset_name)
        deleted_from_chroma = True 
        deletion_messages.append(f"Collection '{dataset_name}' deleted from ChromaDB.")
        print(f"Successfully deleted collection '{dataset_name}' from ChromaDB.")
    except Exception as e:
        # Check if the collection still exists. If not, it's effectively deleted.
        try:
            chroma_client.get_collection(name=dataset_name)
            # If it's still here, then the deletion failed for a more serious reason
            error_msg = f"Failed to delete collection '{dataset_name}' from ChromaDB: {str(e)}"
            deletion_messages.append(error_msg)
            print(error_msg)
        except Exception:
            # Collection was not found by get_collection, so it's effectively gone from Chroma
            deleted_from_chroma = True 
            msg = f"Collection '{dataset_name}' not found in ChromaDB (considered successfully removed)."
            deletion_messages.append(msg)
            print(msg)
            
    # Attempt to delete from metadata
    dataset_metadata = load_dataset_metadata()
    if dataset_name in dataset_metadata:
        del dataset_metadata[dataset_name]
        save_dataset_metadata(dataset_metadata)
        deleted_from_metadata = True
        msg = f"Dataset '{dataset_name}' deleted from metadata."
        deletion_messages.append(msg)
        print(msg)
    else:
        deleted_from_metadata = True # Not present is as good as deleted for this step
        msg = f"Dataset '{dataset_name}' not found in metadata (considered successfully removed from metadata)."
        deletion_messages.append(msg)
        print(msg)

    if deleted_from_chroma and deleted_from_metadata:
        return jsonify({"success": True, "message": f"Dataset '{dataset_name}' successfully deleted. Details: {' '.join(deletion_messages)}"})
    else:
        # Even if one part failed but the other succeeded, it's a partial success from user POV if dataset is gone from list.
        # The key is whether it will be gone from the UI. If metadata is deleted, it will be.
        if deleted_from_metadata:
             return jsonify({"success": True, "message": f"Dataset '{dataset_name}' removed. Status: {' '.join(deletion_messages)}"})
        else:
            # This implies metadata deletion failed, which is less likely.
            return jsonify({"error": f"Failed to fully delete dataset '{dataset_name}'. Status: {' '.join(deletion_messages)}"}), 500

@app.route('/api/datasets/<dataset_name>', methods=['PUT'])
def update_dataset_metadata_route(dataset_name):
    """Update metadata for an existing dataset."""
    data_to_update = request.json
    if not data_to_update:
        return jsonify({"error": "No data provided for update"}), 400

    dataset_metadata = load_dataset_metadata()

    if dataset_name not in dataset_metadata:
        # Also check if it's a default dataset, which shouldn't be "updated" via this mechanism
        if any(d['name'] == dataset_name for d in DEFAULT_DATASETS):
             return jsonify({"error": "Default datasets cannot be modified."}), 403
        return jsonify({"error": "Dataset not found"}), 404

    # Update only the allowed fields
    # Ensure all expected fields are present even if empty, to allow clearing them
    current_meta = dataset_metadata.get(dataset_name, {})
    current_meta["description"] = data_to_update.get("description", current_meta.get("description", ""))
    current_meta["author"] = data_to_update.get("author", current_meta.get("author", ""))
    current_meta["topic"] = data_to_update.get("topic", current_meta.get("topic", ""))
    current_meta["last_update_date"] = datetime.now().isoformat()  # Update the last update date
    
    linkedin_url = data_to_update.get("linkedin_url", "")
    if linkedin_url:
        current_meta["linkedin_url"] = linkedin_url
    else:
        current_meta.pop("linkedin_url", None)
        
    custom_instructions = data_to_update.get("custom_instructions", "")
    if custom_instructions:
        current_meta["custom_instructions"] = custom_instructions
    else:
        current_meta.pop("custom_instructions", None)

    dataset_metadata[dataset_name] = current_meta
    save_dataset_metadata(dataset_metadata)

    return jsonify({"success": True, "message": f"Dataset '{dataset_name}' updated successfully."})

@app.route('/api/process-documents', methods=['POST'])
def process_documents():
    """Process uploaded documents and create embeddings."""
    # This would handle file uploads in production
    # For now, we'll process the existing documents
    
    dataset_name = request.json.get('dataset_name', 'EU-Sanctions')
    
    # Create or get collection
    collection = chroma_client.get_or_create_collection(name=dataset_name)
    
    # Process PDF files
    documents = []
    metadatas = []
    ids = []
    
    # Check if directory exists
    if not os.path.exists(DOCUMENTS_DIR):
        os.makedirs(DOCUMENTS_DIR, exist_ok=True)
        return jsonify({
            "status": "error",
            "message": f"Documents directory not found. Created: {DOCUMENTS_DIR}. Please add PDF files to this directory.",
            "dataset": dataset_name
        })
    
    # List document files (PDF and TXT)
    doc_files = [f for f in os.listdir(DOCUMENTS_DIR) if f.endswith(('.pdf', '.txt'))][:10]
    
    # Handle case where no documents exist
    if not doc_files:
        # Add some sample data for testing
        sample_text = "This is a sample document for the EU Sanctions dataset. The European Union imposes sanctions or restrictive measures in pursuit of the specific objectives of the Common Foreign and Security Policy (CFSP). Sanctions are preventative, non-punitive instruments which aim to bring about a change in policy or activity by targeting non-EU countries, entities, and individuals responsible for the malign behavior at stake."
        documents.append(sample_text)
        metadatas.append({"source": "sample_document.txt", "page": 1})
        ids.append(f"sample_document_0_1")
    
    # Process document files
    for i, doc_file in enumerate(doc_files):
        file_path = os.path.join(DOCUMENTS_DIR, doc_file)
        
        try:
            if doc_file.endswith('.pdf'):
                # Use robust PDF extraction method
                extracted_texts = robust_extract_text_from_pdf(file_path)
                
                # Process extracted texts
                for j, text in enumerate(extracted_texts):
                    if text and len(text) > 50:  # Skip very short segments
                        documents.append(text)
                        metadatas.append({"source": doc_file, "page": j})
                        ids.append(f"{doc_file}_{i}_{j}")
            elif doc_file.endswith('.txt'):
                # Read text file directly
                with open(file_path, 'r', encoding='utf-8') as f:
                    text = f.read()
                    
                # Split text into paragraphs or chunks
                chunks = [t.strip() for t in text.split('\n\n') if t.strip()]
                
                for j, chunk in enumerate(chunks):
                    if len(chunk) > 50:  # Skip very short segments
                        documents.append(chunk)
                        metadatas.append({"source": doc_file, "part": j})
                        ids.append(f"{doc_file}_{i}_{j}")
                        
                # If no chunks (single paragraph), add the whole text
                if not chunks and len(text) > 50:
                    documents.append(text)
                    metadatas.append({"source": doc_file, "part": 0})
                    ids.append(f"{doc_file}_{i}_0")
                    
        except Exception as e:
            print(f"Error processing {file_path}: {str(e)}")
    
    # Add to Chroma
    # In real implementation, use embeddings model to vectorize
    for i in range(0, len(documents), 100):  # Batch processing
        batch_docs = documents[i:i+100]
        batch_meta = metadatas[i:i+100]
        batch_ids = ids[i:i+100]
        
        collection.add(
            documents=batch_docs,
            metadatas=batch_meta,
            ids=batch_ids
        )
    
    # Store dataset description
    dataset_description = request.json.get('dataset_description', '')
    if dataset_description:
        dataset_metadata = load_dataset_metadata()
        dataset_metadata[dataset_name] = {"description": dataset_description}
        save_dataset_metadata(dataset_metadata)
    
    return jsonify({
        "status": "success",
        "message": f"Processed {len(documents)} text chunks from {len(doc_files)} documents",
        "dataset": dataset_name
    })

@app.route('/api/upload-documents', methods=['POST'])
def upload_documents():
    """Handle file uploads and process them."""
    try:
        if 'files' not in request.files:
            return jsonify({"status": "error", "message": "No files uploaded"}), 400
        
        # Get dataset name and sanitize it for ChromaDB requirements
        dataset_name = request.form.get('dataset_name', 'New Dataset')
        dataset_description = request.form.get('dataset_description', '')
        dataset_author = request.form.get('dataset_author', '') # New field
        dataset_topic = request.form.get('dataset_topic', '')   # New field
        dataset_linkedin = request.form.get('dataset_linkedin', '') # New field
        dataset_custom_instructions = request.form.get('dataset_custom_instructions', '') # New field
        
        # Sanitize the dataset name to meet ChromaDB requirements
        # 1. Replace spaces with hyphens
        # 2. Remove special characters except alphanumeric, underscore, and hyphen
        # 3. Ensure it doesn't start or end with non-alphanumeric characters
        sanitized_name = ''.join(c if c.isalnum() or c in '-_' else '-' for c in dataset_name)
        sanitized_name = sanitized_name.strip('-_')  # Remove leading/trailing hyphens and underscores
        
        # If the name is too long, truncate it
        if len(sanitized_name) > 60:
            sanitized_name = sanitized_name[:60]
            
        # If the name is too short, append a timestamp
        if len(sanitized_name) < 3:
            sanitized_name = f"dataset-{int(time.time())}"
            
        # Ensure first and last characters are alphanumeric
        if not sanitized_name[0].isalnum():
            sanitized_name = 'x' + sanitized_name[1:]
        if not sanitized_name[-1].isalnum():
            sanitized_name = sanitized_name[:-1] + 'x'
            
        dataset_name = sanitized_name
        
        # Create a timestamp folder to avoid filename conflicts
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        upload_path = os.path.join(app.config['UPLOAD_FOLDER'], timestamp)
        os.makedirs(upload_path, exist_ok=True)
        
        # Save uploaded files
        uploaded_files = request.files.getlist('files')
        saved_files = []
        
        for file in uploaded_files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file_path = os.path.join(upload_path, filename)
                file.save(file_path)
                saved_files.append(filename)
        
        if not saved_files:
            return jsonify({"status": "error", "message": "No valid files uploaded"}), 400
        
        # Create or get collection
        collection = chroma_client.get_or_create_collection(name=dataset_name)
        
        # Process uploaded files
        documents = []
        metadatas = []
        ids = []
        
        # Process the files (reuse the same code we already have)
        for i, doc_file in enumerate(saved_files):
            file_path = os.path.join(upload_path, doc_file)
            
            try:
                if doc_file.endswith('.pdf'):
                    # Use robust PDF extraction method
                    extracted_texts = robust_extract_text_from_pdf(file_path)
                    print(f"Extracted {len(extracted_texts)} text chunks from {doc_file}")
                    
                    # Process extracted texts
                    for j, text in enumerate(extracted_texts):
                        if text and len(text) > 50:  # Skip very short segments
                            documents.append(text)
                            metadatas.append({"source": doc_file, "page": j})
                            ids.append(f"{doc_file}_{i}_{j}")
                elif doc_file.endswith('.txt'):
                    # Read text file directly
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        text = f.read()
                        
                    # Split text into paragraphs or chunks
                    chunks = [t.strip() for t in text.split('\n\n') if t.strip()]
                    print(f"Split text file into {len(chunks)} chunks")
                    
                    for j, chunk in enumerate(chunks):
                        if len(chunk) > 50:  # Skip very short segments
                            documents.append(chunk)
                            metadatas.append({"source": doc_file, "part": j})
                            ids.append(f"{doc_file}_{i}_{j}")
                            
                    # If no chunks (single paragraph), add the whole text
                    if not chunks and len(text) > 50:
                        documents.append(text)
                        metadatas.append({"source": doc_file, "part": 0})
                        ids.append(f"{doc_file}_{i}_0")
                        
            except Exception as e:
                print(f"Error processing {file_path}: {str(e)}")
        
        print(f"Total documents to process: {len(documents)}")
        
        # Add to Chroma
        for i in range(0, len(documents), 100):  # Batch processing
            batch_docs = documents[i:i+100]
            batch_meta = metadatas[i:i+100]
            batch_ids = ids[i:i+100]
            
            print(f"Processing batch {i//100 + 1} of {(len(documents)-1)//100 + 1}")
            collection.add(
                documents=batch_docs,
                metadatas=batch_meta,
                ids=batch_ids
            )
            print(f"Successfully added batch {i//100 + 1} to ChromaDB")
        
        # Store dataset metadata
        dataset_metadata = load_dataset_metadata()
        current_metadata = dataset_metadata.get(dataset_name, {})
        current_metadata["description"] = dataset_description
        current_metadata["author"] = dataset_author
        current_metadata["topic"] = dataset_topic
        current_metadata["last_update_date"] = datetime.now().isoformat()  # Add last update date
        if dataset_linkedin:
            current_metadata["linkedin_url"] = dataset_linkedin
        else: 
            current_metadata.pop("linkedin_url", None) # Remove if empty or not provided
        
        if dataset_custom_instructions: # New field
            current_metadata["custom_instructions"] = dataset_custom_instructions
        else:
            current_metadata.pop("custom_instructions", None) # Remove if not provided
            
        dataset_metadata[dataset_name] = current_metadata
        save_dataset_metadata(dataset_metadata)
        
        # Check if we actually got any text from the documents
        if len(documents) == 0:
            return jsonify({
                "status": "error",
                "message": "Could not extract any text from the uploaded documents. Please check file format and contents.",
                "dataset": dataset_name,
                "files": saved_files
            }), 400
        
        # Include both original and sanitized names
        original_name = request.form.get('dataset_name', 'New Dataset')
        return jsonify({
            "status": "success",
            "message": f"Processed {len(documents)} text chunks from {len(saved_files)} documents",
            "dataset": dataset_name,
            "original_name": original_name,
            "files": saved_files
        })
    except Exception as e:
        print(f"Error in upload_documents: {str(e)}")
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"}), 500

# Folder API routes
@app.route('/api/folders', methods=['GET'])
def list_folders_route():
    """List all folders."""
    folders = chat_storage.list_folders()
    return jsonify(folders)

@app.route('/api/folders', methods=['POST'])
def create_folder_route():
    """Create a new folder."""
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Missing folder name'}), 400
    folder_id = chat_storage.create_folder(name)
    return jsonify({'success': True, 'id': folder_id, 'name': name})

@app.route('/api/folders/<folder_id>', methods=['PUT'])
def rename_folder_route(folder_id):
    """Rename a folder."""
    data = request.json
    new_name = data.get('name', '').strip()
    if not new_name:
        return jsonify({'error': 'Missing new folder name'}), 400

    # Load folders
    folders = chat_storage.list_folders()
    found = False
    for folder in folders:
        if folder['id'] == folder_id:
            folder['name'] = new_name
            folder['last_updated'] = time.time()
            found = True
            break
    if not found:
        return jsonify({'error': 'Folder not found'}), 404

    # Save folders
    # chat_storage does not have a save_folders, so we update the file directly
    import os, json
    folders_file = os.path.join(chat_storage.storage_dir, "folders.json")
    with open(folders_file, 'w') as f:
        json.dump({"folders": folders}, f, indent=2)

    return jsonify({'success': True})

@app.route('/api/folders/<folder_id>', methods=['DELETE'])
def delete_folder_route(folder_id):
    """Delete a folder and move its chats to the default folder."""
    if folder_id == 'default':
        return jsonify({'error': 'Cannot delete the default folder'}), 400

    # First, move all chats in this folder to the default folder
    chats = chat_storage.list_chats()
    for chat in chats:
        if chat.get('folder_id') == folder_id:
            chat_storage.move_chat_to_folder(chat['id'], 'default')

    # Then delete the folder
    success = chat_storage.delete_folder(folder_id)
    if not success:
        return jsonify({'error': 'Folder not found'}), 404

    return jsonify({'success': True})

# Chat API routes
@app.route('/api/chats', methods=['GET'])
def list_chats():
    """List all chats."""
    folder_id = request.args.get('folder_id')
    chats = chat_storage.list_chats(folder_id)
    return jsonify(chats)

@app.route('/api/chats', methods=['POST'])
def create_chat():
    """Create a new chat."""
    data = request.json
    title = data.get('title')
    folder_id = data.get('folder_id', 'default')
    dataset = data.get('dataset')
    
    chat_id = chat_storage.create_chat(title, folder_id)
    
    # Update dataset if provided
    if dataset:
        chat_storage.update_chat(chat_id, {"dataset": dataset})
    
    return jsonify({"id": chat_id})

@app.route('/api/chats/<chat_id>/move', methods=['POST'])
def move_chat_to_folder_route(chat_id):
    """Move a chat to a specified folder."""
    data = request.json
    folder_id = data.get('folder_id')

    if folder_id is None:
        return jsonify({"error": "Missing folder_id"}), 400

    # Optional: Check if the target folder exists (though chat_storage.move currently doesn't)
    # folders = chat_storage.list_folders()
    # if not any(f['id'] == folder_id for f in folders):
    #     return jsonify({"error": f"Target folder {folder_id} not found"}), 404

    success = chat_storage.move_chat_to_folder(chat_id, folder_id)
    if not success:
        # This implies chat_id was not found by chat_storage.update_chat
        return jsonify({"error": f"Chat {chat_id} not found or failed to update."}), 404 
    
    return jsonify({"success": True, "message": f"Chat {chat_id} moved to folder {folder_id}"})

@app.route('/api/chats/<chat_id>', methods=['GET'])
def get_chat(chat_id):
    """Get a chat by ID."""
    chat = chat_storage.get_chat(chat_id)
    if chat is None:
        return jsonify({"error": "Chat not found"}), 404
    
    # Validate and sanitize messages to ensure they're correctly formatted
    if 'messages' in chat:
        # Log message count for debugging
        print(f"Chat {chat_id} has {len(chat['messages'])} messages")
        
        # Filter out any messages with missing/invalid content
        valid_messages = []
        for msg in chat['messages']:
            # Ensure messages have required fields
            if 'role' not in msg or 'content' not in msg or not msg['content']:
                print(f"Skipping invalid message in chat {chat_id}: {msg}")
                continue
                
            # Ensure role is valid
            if msg['role'] not in ['user', 'assistant', 'system']:
                print(f"Invalid role in message: {msg['role']}")
                msg['role'] = 'system'  # Default to system for invalid roles
                
            # Ensure assistant messages have proper formatting
            if msg['role'] == 'assistant' and not msg.get('metadata', {}).get('isSystem', False):
                # Check if AI response has the expected format
                has_sources = "SOURCES:" in msg['content']
                has_analysis = "ANALYSIS:" in msg['content']
                
                # If message doesn't have expected format, log it
                if not (has_sources or has_analysis):
                    print(f"Assistant message lacks expected sections: {msg['content'][:100]}...")
            
            valid_messages.append(msg)
            
        chat['messages'] = valid_messages
    
    return jsonify(chat)

@app.route('/api/chats/<chat_id>', methods=['PUT'])
def update_chat_route(chat_id):
    """Update a chat."""
    data = request.json
    success = chat_storage.update_chat(chat_id, data)
    
    if not success:
        return jsonify({"error": "Chat not found"}), 404
    
    return jsonify({"success": True})

@app.route('/api/chats/<chat_id>', methods=['DELETE'])
def delete_chat(chat_id):
    """Delete a chat and return the next most recent chat."""
    result = chat_storage.delete_chat(chat_id)
    
    if not result["success"]:
        return jsonify({"error": result["message"]}), 404
    
    return jsonify({
        "success": True,
        "next_chat": result.get("next_chat"),
        "message": result["message"]
    })

@app.route('/api/chats/<chat_id>/messages', methods=['POST'])
def add_message(chat_id):
    print(f"[CHAT_API] add_message called for chat_id: {chat_id}")
    data = request.json
    print(f"[CHAT_API] Request data: {data}")
    user_message = data.get('message', '')
    dataset_name = data.get('dataset')
    
    print("[CHAT_API] Attempting to get chat from storage...")
    chat = chat_storage.get_chat(chat_id)
    if chat is None:
        print(f"[CHAT_API] Chat {chat_id} not found.")
        return jsonify({"error": "Chat not found"}), 404
    print(f"[CHAT_API] Chat {chat_id} retrieved.")
    
    # Use the chat's dataset if not specified
    if not dataset_name:
        dataset_name = chat.get('dataset', 'Default')
    elif dataset_name != chat.get('dataset'):
        # Update the chat's dataset if it changed
        chat_storage.update_chat(chat_id, {"dataset": dataset_name})
    
    # Load dataset-specific custom instructions
    dataset_metadata = load_dataset_metadata()
    specific_dataset_meta = dataset_metadata.get(dataset_name, {})
    custom_instructions_for_dataset = specific_dataset_meta.get("custom_instructions", "")
    
    # Add user message
    print("[CHAT_API] Attempting to add user message to storage...")
    chat_storage.add_message(chat_id, "user", user_message)
    print("[CHAT_API] User message added to storage.")
    
    # Extract chat history for context
    history = []
    for msg in chat.get('messages', []):
        if msg['role'] in ['user', 'assistant']:
            history.append({"role": msg['role'], "content": msg['content']})
    
    # Retrieve context from Chroma based on user query
    collection = chroma_client.get_or_create_collection(name=dataset_name)
    
    # Get the document count to determine how many results we can request
    doc_count = collection.count()
    n_results = min(5, max(1, doc_count))  # Request at most 5, but at least 1 if available
    
    # Prepare results placeholder
    if doc_count == 0:
        context = "No documents found in the selected dataset. Please upload documents or select a different dataset."
        sources = [{"source": "System", "page": 0, "snippet": "No documents available"}]
        print("[CHAT_API] No documents in dataset, using default context.")
    else:
        print("[CHAT_API] Attempting to query dataset for context...")
        from app.utils.document_processor import LegalDocumentProcessor
        
        # Initialize document processor with the same settings as configured globally
        doc_processor = LegalDocumentProcessor(
            embedding_model=EMBEDDING_MODEL,
            chroma_path=CHROMA_DIR,
            device=device
        )
        
        # Use advanced query with hybrid search and reranking
        results = doc_processor.query_dataset(
            dataset_name=dataset_name,
            query=user_message,
            n_results=n_results,
            use_hybrid_search=True,
            use_reranking=True
        )
        print("[CHAT_API] Dataset query complete.")
        
        context = '\n\n'.join(results['documents'][0])
        
        # Extract sources information for better citation
        sources = []
        for doc, meta in zip(results['documents'][0], results['metadatas'][0]):
            source_info = {
                "source": meta.get("source", "Unknown"),
                "page": meta.get("page", meta.get("part", 0)),
                "snippet": doc[:150] + "..." if len(doc) > 150 else doc  # Short preview
            }
            sources.append(source_info)
    
    # Create enhanced prompt for LLM with Chain of Thought reasoning
    # Define the common suffix for the prompt
    common_prompt_suffix = f"""# Output Format
Structure your response with these sections:
1. SOURCES: A brief bulleted list of the most relevant source documents you're drawing from
2. ANALYSIS: Your step-by-step reasoning about the question (this should be detailed)
3. APPLICABLE PROVISIONS: Specific articles, sections, or legal provisions that apply
4. CONCLUSION: Your final answer based on the analysis

# Context
{context}
"""

    if custom_instructions_for_dataset:
        # Use custom instructions as the main persona/task for the AI for this dataset
        system_message = f"""{custom_instructions_for_dataset}

{common_prompt_suffix}"""
        print(f"[CHAT_API] Using custom instructions for dataset {dataset_name}: {custom_instructions_for_dataset[:100]}...")
    else:
        # Default system message if no custom instructions for the dataset
        default_general_instructions = f"""You are a legal expert specializing in international sanctions regulations.

# Instructions
- Use the provided context to analyze the user's question thoroughly
- Implement chain-of-thought reasoning by breaking down your analysis step by step
- First carefully examine the relevant sections from the provided context
- Think about what legal principles apply to this situation
- Consider multiple perspectives and interpretations if applicable
- Draw connections between different parts of the context
- Formulate a comprehensive and legally sound analysis
- Cite specific articles, sections, or provisions when possible
- Clearly separate your reasoning process from your final conclusion
- If you don't know the answer or it's not in the context, state this clearly"""
        system_message = f"""{default_general_instructions}

{common_prompt_suffix}"""
        print(f"[CHAT_API] Using default system instructions for dataset {dataset_name}")
    
    print(f"[CHAT_API] FINAL System Prompt for LLM:\n------\n{system_message}\n------")

    # Initialize variables for response tracking
    full_response = ""
    saved_response_length = 0
    message_id = f"msg_{int(time.time())}_{os.urandom(4).hex()}"
    
    # Create a placeholder message for the assistant to start with
    metadata = {
        "dataset": dataset_name,
        "sources": sources,
        "streaming": True  # Flag to indicate this is an in-progress streaming message
    }
    
    # Add placeholder message that will be updated as streaming progresses
    print("[CHAT_API] Attempting to add assistant placeholder message to storage...")
    chat_storage.add_message(chat_id, "assistant", "Generating response...", metadata)
    print("[CHAT_API] Assistant placeholder message added.")
    
    # Now get the updated chat to find the message ID
    updated_chat = chat_storage.get_chat(chat_id)
    if updated_chat and updated_chat.get('messages'):
        # Find the placeholder message we just added
        for msg in reversed(updated_chat.get('messages', [])):
            if msg.get('role') == 'assistant' and msg.get('metadata', {}).get('streaming'):
                message_id = msg.get('id')
                break
    
    print(f"Created placeholder message with ID: {message_id}")
    
    # Function to update the message in storage with current content
    def update_streaming_message(current_content):
        # Get the current chat 
        current_chat = chat_storage.get_chat(chat_id)
        if not current_chat:
            return False
            
        # Find the message to update
        found = False
        for i, msg in enumerate(current_chat.get('messages', [])):
            if msg.get('id') == message_id:
                # Update the message content
                current_chat['messages'][i]['content'] = current_content
                # Keep the streaming flag to indicate it's still in progress
                found = True
                break
                
        if found:
            # Write the updated chat back to storage
            chat_file = os.path.join(chat_storage.storage_dir, f"{chat_id}.json")
            with open(chat_file, 'w') as f:
                json.dump(current_chat, f, indent=2)
            return True
        
        return False
    
    # Function to finalize the message when streaming is complete
    def finalize_message(final_content):
        # Get the current chat
        current_chat = chat_storage.get_chat(chat_id)
        if not current_chat:
            return False
            
        # Find the message to update
        found = False
        for i, msg in enumerate(current_chat.get('messages', [])):
            if msg.get('id') == message_id:
                # Update with final content
                current_chat['messages'][i]['content'] = final_content
                # Remove the streaming flag
                if 'streaming' in current_chat['messages'][i]['metadata']:
                    del current_chat['messages'][i]['metadata']['streaming']
                found = True
                break
                
        if found:
            # Update timestamps
            current_chat['updated_at'] = time.time()
            
            # Auto-name the chat based on the assistant response
            # Only if this was the first response (chat had only the user message before)
            if len(current_chat.get('messages', [])) == 2:  # User + AI response
                # Always generate a title from the AI response, regardless of current title
                new_title = ""
                
                # Try to get title from conclusion first
                if "CONCLUSION:" in final_content:
                    title_text = final_content.split("CONCLUSION:")[1].strip()
                    new_title = title_text.split("\n")[0][:50]
                # Then try analysis
                elif "ANALYSIS:" in final_content:
                    title_text = final_content.split("ANALYSIS:")[1].strip()
                    new_title = title_text.split("\n")[0][:50]
                # Fallback to first line
                else:
                    new_title = final_content.split("\n")[0][:50]
                
                # Add ellipsis if truncated
                if len(new_title) == 50:
                    new_title += "..."
                
                # Only update if we got a meaningful title
                if new_title.strip():
                    print(f"Updating chat title to: {new_title}")
                    current_chat['title'] = new_title
            
            # Write the updated chat back to storage
            chat_file = os.path.join(chat_storage.storage_dir, f"{chat_id}.json")
            with open(chat_file, 'w') as f:
                json.dump(current_chat, f, indent=2)
            
            print(f"Finalized message {message_id} with {len(final_content)} chars")
            return True
        
        return False
    
    def generate():
        nonlocal full_response, saved_response_length
        
        try:
            # Start with an empty string event to establish connection
            yield "data: \n\n"
            
            print("[CHAT_API] In generate(): About to call llm_client.stream_with_rag")
            # Stream the LLM response
            for chunk in llm_client.stream_with_rag(
                query=user_message,
                context=system_message,
                chat_history=history[-10:] if history else [],
                temperature=0.1
            ):
                print(f"[CHAT_API] In generate(): Received chunk: {chunk[:50]}...")
                full_response += chunk
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                
                if len(full_response) - saved_response_length > 200:
                    update_streaming_message(full_response)
                    saved_response_length = len(full_response)
                    print(f"[CHAT_API] In generate(): Updated streaming message, now at {saved_response_length} chars")
            
            print("[CHAT_API] In generate(): Finished iterating llm_client.stream_with_rag")
            # Yield a completion event 
            yield f"data: {json.dumps({'done': True})}\n\n"
            
            # Make sure to save the final complete response
            finalize_message(full_response)
            
        except Exception as e:
            error_message = f"Error in streaming: {str(e)}"
            print(error_message)
            error_msg = "An error occurred during streaming."
            
            # Try to save what we have so far
            if full_response:
                finalize_message(full_response + "\n\n[Streaming was interrupted]")
            else:
                finalize_message("An error occurred while generating the response.")
                
            yield f"data: {json.dumps({'error': error_msg})}\n\n"
    
    # The @after_this_request handler was unreliable for saving messages
    # We now save during streaming instead
    
    print("[CHAT_API] Returning streaming response object.")
    # Return streaming response
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"  # Disable Nginx buffering
        }
    )

@app.route('/api/save-last-chat', methods=['POST'])
def save_last_chat():
    """Save the last active chat ID in the user's session."""
    data = request.json
    chat_id = data.get('chat_id')
    
    if not chat_id:
        return jsonify({"error": "No chat ID provided"}), 400
    
    # Make sure session is permanent
    session.permanent = True
    
    # Save the chat ID in the session
    session['last_active_chat'] = chat_id
    
    return jsonify({"success": True})

# Authentication, user management, and security routes

# Helper function to get current user and verify permissions
def get_current_user():
    """Get the current authenticated user."""
    try:
        # Verify JWT token is valid
        verify_jwt_in_request()
        
        # Get user ID from JWT token
        user_id = get_jwt_identity()
        
        # Get user from database
        user = user_manager.get_user(user_id)
        
        return user
    except Exception:
        return None

def check_permission(permission):
    """Check if the current user has the specified permission."""
    user = get_current_user()
    
    if not user:
        return False
    
    role = user.get("role")
    if not role:
        return False
    
    permissions = ROLE_PERMISSIONS.get(role, [])
    return permission in permissions

# Authentication middleware for routes
def auth_required(permission=None):
    """Decorator for routes that require authentication."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_current_user()
            
            if not user:
                return jsonify({"error": "Authentication required"}), 401
            
            # Check if user has the required permission
            if permission and not check_permission(permission):
                return jsonify({"error": "Permission denied"}), 403
            
            # Add user to flask.g
            g.user = user
            
            # Log the access
            audit_logger.log_access(
                user_id=user.get("id"),
                resource_id=request.path,
                resource_type="api",
                action=request.method.lower(),
                status="success",
                ip_address=request.remote_addr,
                session_id=request.cookies.get('session')
            )
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# Auth routes
@app.route('/api/auth/register', methods=['POST'])
def register():
    """Register a new user."""
    data = request.json
    
    # Validate required fields
    required_fields = ['username', 'email', 'password']
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400
    
    # Extract fields
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')
    
    # Create user
    user_id = user_manager.create_user(username, email, password)
    
    if not user_id:
        return jsonify({"error": "Username or email already exists"}), 400
    
    # Log the registration
    audit_logger.log_authentication(
        user_id=user_id,
        action="register",
        status="success",
        ip_address=request.remote_addr,
        session_id=request.cookies.get('session')
    )
    
    return jsonify({
        "success": True,
        "message": "User registered successfully"
    })

@app.route('/api/auth/login', methods=['POST'])
def login():
    """Log in a user."""
    data = request.json
    
    # Validate required fields
    if 'username_or_email' not in data or not data['username_or_email']:
        return jsonify({"error": "Username or email is required"}), 400
    
    if 'password' not in data or not data['password']:
        return jsonify({"error": "Password is required"}), 400
    
    # Extract fields
    username_or_email = data.get('username_or_email')
    password = data.get('password')
    
    # Authenticate user
    success, user, error_message = user_manager.authenticate(username_or_email, password)
    
    if not success:
        # Log failed login attempt
        audit_logger.log_authentication(
            user_id=None,
            action="login",
            status="failure",
            ip_address=request.remote_addr,
            session_id=request.cookies.get('session'),
            details={"error": error_message, "attempted_login": username_or_email}
        )
        
        return jsonify({"error": error_message}), 401
    
    # Check if MFA is required
    if error_message == "MFA required":
        return jsonify({
            "mfa_required": True,
            "user_id": user["id"]
        })
    
    # Generate tokens
    tokens = user_manager.generate_tokens(user["id"])
    
    # Log successful login
    audit_logger.log_authentication(
        user_id=user["id"],
        action="login",
        status="success",
        ip_address=request.remote_addr,
        session_id=request.cookies.get('session')
    )
    
    # Return user data and tokens
    return jsonify({
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "role": user["role"],
            "profile": user.get("profile", {})
        },
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"]
    })

@app.route('/api/auth/verify-mfa', methods=['POST'])
def verify_mfa():
    """Verify MFA code."""
    data = request.json
    
    if 'user_id' not in data or not data['user_id']:
        return jsonify({"error": "User ID is required"}), 400
    
    if 'mfa_code' not in data or not data['mfa_code']:
        return jsonify({"error": "MFA code is required"}), 400
    
    user_id = data.get('user_id')
    mfa_code = data.get('mfa_code')
    
    # Verify MFA code
    if not user_manager.verify_mfa(user_id, mfa_code):
        # Log failed MFA verification
        audit_logger.log_authentication(
            user_id=user_id,
            action="verify_mfa",
            status="failure",
            ip_address=request.remote_addr,
            session_id=request.cookies.get('session')
        )
        
        return jsonify({"error": "Invalid MFA code"}), 401
    
    # Generate tokens
    tokens = user_manager.generate_tokens(user_id)
    
    # Get user data
    user = user_manager.get_user(user_id)
    
    # Log successful MFA verification
    audit_logger.log_authentication(
        user_id=user_id,
        action="verify_mfa",
        status="success",
        ip_address=request.remote_addr,
        session_id=request.cookies.get('session')
    )
    
    # Return user data and tokens
    return jsonify({
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "role": user["role"],
            "profile": user.get("profile", {})
        },
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"]
    })

@app.route('/api/auth/refresh', methods=['POST'])
def refresh_token():
    """Refresh the access token using a refresh token."""
    try:
        # Get the refresh token from the request
        refresh_token = request.json.get('refresh_token')
        
        if not refresh_token:
            return jsonify({"error": "Refresh token is required"}), 400
        
        # Verify the refresh token
        from flask_jwt_extended import decode_token
        
        try:
            # Decode the refresh token to get the user ID
            token_data = decode_token(refresh_token)
            user_id = token_data["sub"]
            
            # Check if the user exists and is active
            user = user_manager.get_user(user_id)
            
            if not user or not user.get("is_active", False):
                return jsonify({"error": "User not found or inactive"}), 401
            
            # Generate a new access token
            tokens = user_manager.generate_tokens(user_id)
            
            # Log token refresh
            audit_logger.log_authentication(
                user_id=user_id,
                action="token_refresh",
                status="success",
                ip_address=request.remote_addr,
                session_id=request.cookies.get('session')
            )
            
            return jsonify({
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"]
            })
            
        except Exception as e:
            return jsonify({"error": "Invalid refresh token"}), 401
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/auth/logout', methods=['POST'])
@auth_required()
def logout():
    """Log out a user by blacklisting their tokens."""
    # In a real implementation, we would add the tokens to a blacklist
    # For now, we'll just log the logout
    user = g.user
    
    audit_logger.log_authentication(
        user_id=user.get("id"),
        action="logout",
        status="success",
        ip_address=request.remote_addr,
        session_id=request.cookies.get('session')
    )
    
    return jsonify({"success": True})

# User management routes
@app.route('/api/users/me', methods=['GET'])
@auth_required()
def get_current_user_profile():
    """Get the current user's profile."""
    user = g.user
    
    # Get user credits
    credits = credit_system.get_user_balance(user["id"])
    
    return jsonify({
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "role": user["role"],
        "credits": credits,
        "profile": user.get("profile", {})
    })

@app.route('/api/users/me', methods=['PUT'])
@auth_required()
def update_current_user_profile():
    """Update the current user's profile."""
    user = g.user
    data = request.json
    
    # Only allow updating specific fields
    allowed_fields = ['profile']
    update_data = {k: v for k, v in data.items() if k in allowed_fields}
    
    # Update user
    success = user_manager.update_user(user["id"], update_data)
    
    if not success:
        return jsonify({"error": "Failed to update user"}), 500
    
    return jsonify({"success": True})

@app.route('/api/users/change-password', methods=['POST'])
@auth_required()
def change_password():
    """Change the current user's password."""
    user = g.user
    data = request.json
    
    if 'current_password' not in data or not data['current_password']:
        return jsonify({"error": "Current password is required"}), 400
    
    if 'new_password' not in data or not data['new_password']:
        return jsonify({"error": "New password is required"}), 400
    
    current_password = data.get('current_password')
    new_password = data.get('new_password')
    
    # Change password
    success, error_message = user_manager.change_password(user["id"], current_password, new_password)
    
    if not success:
        return jsonify({"error": error_message}), 400
    
    # Log password change
    audit_logger.log_data_event(
        user_id=user["id"],
        resource_id=user["id"],
        resource_type="user",
        action="change_password",
        status="success",
        ip_address=request.remote_addr,
        session_id=request.cookies.get('session')
    )
    
    return jsonify({"success": True})

# Secure document routes
@app.route('/api/secure-documents/upload', methods=['POST'])
@auth_required("write")
def upload_secure_document():
    """Upload and process a document securely."""
    user = g.user
    
    # Check if file was uploaded
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    # Check file extension
    if not allowed_file(file.filename):
        return jsonify({"error": "File type not allowed"}), 400
    
    # Get dataset name from request
    dataset_name = request.form.get('dataset', 'Default')
    
    # Ensure we have enough credits
    processing_cost = 5  # Example: 5 credits per document
    if not credit_system.check_can_afford(user["id"], processing_cost):
        return jsonify({"error": "Insufficient credits"}), 402  # Payment Required
    
    try:
        # Process document in memory without saving to disk
        result = secure_processor.memoryless_processing(
            file_obj=file.stream,
            dataset_name=dataset_name,
            original_filename=file.filename,
            user_id=user["id"]
        )
        
        # Deduct credits for document processing
        success, new_balance = credit_system.deduct_usage(
            user_id=user["id"],
            amount=processing_cost,
            feature="document_processing",
            description=f"Processing document: {file.filename}"
        )
        
        # Log document upload
        audit_logger.log_data_event(
            user_id=user["id"],
            resource_id=result["encrypted_id"],
            resource_type="document",
            action="upload",
            status="success",
            ip_address=request.remote_addr,
            session_id=request.cookies.get('session'),
            details={"dataset": dataset_name, "filename": file.filename}
        )
        
        return jsonify({
            "status": "success",
            "message": "Document processed securely",
            "document_id": result["encrypted_id"],
            "dataset": dataset_name,
            "credits_used": processing_cost,
            "credits_remaining": new_balance
        })
        
    except Exception as e:
        # Log error
        audit_logger.log_exception(
            exception=e,
            user_id=user["id"],
            resource_type="document",
            action="upload",
            ip_address=request.remote_addr,
            session_id=request.cookies.get('session')
        )
        
        return jsonify({"error": f"Error processing document: {str(e)}"}), 500

@app.route('/api/secure-documents/<document_id>', methods=['GET'])
@auth_required("read")
def get_secure_document(document_id):
    """Get temporary access to a secure document."""
    user = g.user
    
    try:
        # Get secure access to the document
        access = secure_processor.get_document_securely(
            encrypted_id=document_id,
            user_id=user["id"],
            max_age_seconds=300  # 5 minutes
        )
        
        # Log document access
        audit_logger.log_access(
            user_id=user["id"],
            resource_id=document_id,
            resource_type="document",
            action="access",
            status="success",
            ip_address=request.remote_addr,
            session_id=request.cookies.get('session')
        )
        
        return jsonify({
            "access_token": access["access_token"],
            "original_name": access["original_name"],
            "mime_type": access["mime_type"],
            "expires_at": access["expires_at"]
        })
        
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
        
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
        
    except Exception as e:
        return jsonify({"error": f"Error accessing document: {str(e)}"}), 500

@app.route('/api/secure-documents/stream/<access_token>', methods=['GET'])
def stream_secure_document(access_token):
    """Stream a secure document using an access token."""
    try:
        # Stream the document
        file, filename, mimetype = secure_processor.stream_document_securely(access_token)
        
        # Set up response
        response = Response(
            stream_with_context(file.read()),
            mimetype=mimetype
        )
        
        # Set content disposition header to make the browser download the file
        response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
        
        # Make sure to close the file when the response is complete
        @after_this_request
        def cleanup(response):
            file.close()
            return response
        
        return response
        
    except PermissionError:
        return jsonify({"error": "Invalid or expired access token"}), 403
        
    except Exception as e:
        return jsonify({"error": f"Error streaming document: {str(e)}"}), 500

@app.route('/api/secure-documents/<document_id>', methods=['DELETE'])
@auth_required("delete")
def delete_secure_document(document_id):
    """Delete a secure document."""
    user = g.user
    
    try:
        # Delete the document
        success = secure_processor.delete_document_securely(
            encrypted_id=document_id,
            user_id=user["id"]
        )
        
        if not success:
            return jsonify({"error": "Document not found"}), 404
        
        # Log document deletion
        audit_logger.log_data_event(
            user_id=user["id"],
            resource_id=document_id,
            resource_type="document",
            action="delete",
            status="success",
            ip_address=request.remote_addr,
            session_id=request.cookies.get('session')
        )
        
        return jsonify({"success": True})
        
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
        
    except Exception as e:
        return jsonify({"error": f"Error deleting document: {str(e)}"}), 500

# Credit system routes
@app.route('/api/credits/balance', methods=['GET'])
@auth_required()
def get_credit_balance():
    """Get the current user's credit balance."""
    user = g.user
    
    # Get user credits
    credits = credit_system.get_user_balance(user["id"])
    
    return jsonify({
        "credits": credits
    })

@app.route('/api/credits/history', methods=['GET'])
@auth_required()
def get_credit_history():
    """Get the current user's credit transaction history."""
    user = g.user
    
    # Get transaction history
    history = credit_system.get_transaction_history(user["id"])
    
    return jsonify(history)

@app.route('/api/credits/packages', methods=['GET'])
def get_credit_packages():
    """Get available credit packages for purchase."""
    # Get credit packages
    packages = credit_system.get_credit_packages()
    
    return jsonify(packages)

@app.route('/api/credits/purchase', methods=['POST'])
@auth_required()
def purchase_credits():
    """Purchase credits."""
    user = g.user
    data = request.json
    
    if 'package_id' not in data:
        return jsonify({"error": "Package ID is required"}), 400
    
    package_id = data.get('package_id')
    payment_ref = data.get('payment_ref')  # Optional payment reference
    
    # Purchase the package
    success, message, credits_added = credit_system.purchase_credit_package(
        user_id=user["id"],
        package_id=package_id,
        payment_ref=payment_ref
    )
    
    if not success:
        return jsonify({"error": message}), 400
    
    # Get new balance
    new_balance = credit_system.get_user_balance(user["id"])
    
    # Log the purchase
    audit_logger.log_data_event(
        user_id=user["id"],
        resource_id=package_id,
        resource_type="credit_package",
        action="purchase",
        status="success",
        ip_address=request.remote_addr,
        session_id=request.cookies.get('session'),
        details={"credits_added": credits_added, "payment_ref": payment_ref}
    )
    
    return jsonify({
        "success": True,
        "message": message,
        "credits_added": credits_added,
        "new_balance": new_balance
    })

# Feedback routes
@app.route('/api/user-feedback', methods=['POST'])
def submit_user_feedback():
    """Submit feedback."""
    data = request.json
    
    # Try to get user ID from token if available
    try:
        verify_jwt_in_request(optional=True)
        user_id = get_jwt_identity()
    except Exception:
        user_id = None
    
    # Check required fields
    if 'feedback_type' not in data:
        return jsonify({"error": "Feedback type is required"}), 400
    
    if 'content' not in data:
        return jsonify({"error": "Feedback content is required"}), 400
    
    feedback_type = data.get('feedback_type')
    content = data.get('content')
    rating = data.get('rating')
    
    # Validate the feedback
    is_valid, error_message = feedback_manager.validate_feedback(content, feedback_type, rating)
    
    if not is_valid:
        return jsonify({"error": error_message}), 400
    
    # Sanitize the content
    sanitized_content = feedback_manager.sanitize_feedback(content)
    
    # Submit the feedback
    feedback_id = feedback_manager.submit_feedback(
        user_id=user_id,
        feedback_type=feedback_type,
        content=sanitized_content,
        rating=rating,
        metadata=data.get('metadata')
    )
    
    # Log the feedback submission
    audit_logger.log_data_event(
        user_id=user_id,
        resource_id=feedback_id,
        resource_type="feedback",
        action="submit",
        status="success",
        ip_address=request.remote_addr,
        session_id=request.cookies.get('session')
    )
    
    return jsonify({
        "success": True,
        "feedback_id": feedback_id
    })

# --- DATASET DOCUMENT MANAGEMENT ENDPOINTS ---
from flask import send_from_directory

@app.route('/api/datasets/<dataset_name>/documents', methods=['GET'])
def list_dataset_documents(dataset_name):
    """List all documents in a dataset (by Chroma collection metadata)."""
    try:
        collection = chroma_client.get_collection(name=dataset_name)
        results = collection.get(include=["metadatas"])  # Only metadatas, ids always returned
        print(f"[DEBUG] Chroma collection metadatas for {dataset_name}:")
        for meta, doc_id in zip(results["metadatas"], results["ids"]):
            print(f"  id={doc_id} meta={meta}")
        docs = {}
        for meta, doc_id in zip(results["metadatas"], results["ids"]):
            filename = meta.get("source") or meta.get("file") or meta.get("filename")
            if not filename:
                continue
            if filename not in docs:
                docs[filename] = {"filename": filename, "ids": [], "id": filename}
            docs[filename]["ids"].append(doc_id)
        doc_list = list(docs.values())
        if not doc_list:
            # Return all metadatas for debugging
            return jsonify({"documents": [], "debug_metadatas": results["metadatas"]}), 200
        return jsonify(doc_list)
    except Exception as e:
        print(f"Error listing documents for dataset {dataset_name}: {e}")
        return jsonify([]), 200

@app.route('/api/datasets/<dataset_name>/documents', methods=['POST'])
def upload_documents_to_dataset(dataset_name):
    """Upload documents to an existing dataset (append to Chroma collection)."""
    try:
        if 'files' not in request.files:
            return jsonify({"status": "error", "message": "No files uploaded"}), 400
        # Save uploaded files
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        upload_path = os.path.join(app.config['UPLOAD_FOLDER'], timestamp)
        os.makedirs(upload_path, exist_ok=True)
        uploaded_files = request.files.getlist('files')
        saved_files = []
        for file in uploaded_files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file_path = os.path.join(upload_path, filename)
                file.save(file_path)
                saved_files.append(filename)
        if not saved_files:
            return jsonify({"status": "error", "message": "No valid files uploaded"}), 400
        # Get collection
        collection = chroma_client.get_or_create_collection(name=dataset_name)
        # Process and add to Chroma
        documents = []
        metadatas = []
        ids = []
        for i, doc_file in enumerate(saved_files):
            file_path = os.path.join(upload_path, doc_file)
            try:
                if doc_file.endswith('.pdf'):
                    extracted_texts = robust_extract_text_from_pdf(file_path)
                    for j, text in enumerate(extracted_texts):
                        if text and len(text) > 50:
                            documents.append(text)
                            metadatas.append({"source": doc_file, "page": j})
                            ids.append(f"{doc_file}_{i}_{j}")
                elif doc_file.endswith('.txt'):
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        text = f.read()
                    chunks = [t.strip() for t in text.split('\n\n') if t.strip()]
                    for j, chunk in enumerate(chunks):
                        if len(chunk) > 50:
                            documents.append(chunk)
                            metadatas.append({"source": doc_file, "part": j})
                            ids.append(f"{doc_file}_{i}_{j}")
                    if not chunks and len(text) > 50:
                        documents.append(text)
                        metadatas.append({"source": doc_file, "part": 0})
                        ids.append(f"{doc_file}_{i}_0")
            except Exception as e:
                print(f"Error processing {file_path}: {str(e)}")
        for i in range(0, len(documents), 100):
            batch_docs = documents[i:i+100]
            batch_meta = metadatas[i:i+100]
            batch_ids = ids[i:i+100]
            collection.add(documents=batch_docs, metadatas=batch_meta, ids=batch_ids)
        return jsonify({"success": True, "message": f"Uploaded {len(saved_files)} documents to {dataset_name}"})
    except Exception as e:
        print(f"Error uploading documents to dataset {dataset_name}: {e}")
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"}), 500

@app.route('/api/datasets/<dataset_name>/documents/<doc_id>', methods=['DELETE'])
def delete_document_from_dataset(dataset_name, doc_id):
    """Delete a document and all its chunks from a dataset."""
    try:
        collection = chroma_client.get_collection(name=dataset_name)
        # Find all ids for this document (by filename)
        results = collection.get(include=["metadatas"])  # Only metadatas, ids always returned
        ids_to_delete = [id_ for meta, id_ in zip(results["metadatas"], results["ids"]) if meta.get("source") == doc_id or meta.get("file") == doc_id or meta.get("filename") == doc_id]
        if not ids_to_delete:
            return jsonify({"success": False, "message": "No chunks found for this document."}), 404
        collection.delete(ids=ids_to_delete)
        return jsonify({"success": True, "message": f"Deleted document {doc_id} and its chunks."})
    except Exception as e:
        print(f"Error deleting document {doc_id} from dataset {dataset_name}: {e}")
        return jsonify({"success": False, "message": f"Server error: {str(e)}"}), 500

@app.route('/api/datasets/check-name')
def check_dataset_name():
    """Check if a dataset name is already taken."""
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({"taken": False, "error": "No name provided"}), 400
    collections = chroma_client.list_collections()
    collection_names = [c.name for c in collections]
    if name in collection_names:
        return jsonify({"taken": True})
    return jsonify({"taken": False})

# Patch /api/upload-documents to check for existing name
import functools
orig_upload_documents = upload_documents
@functools.wraps(orig_upload_documents)
def upload_documents_patched():
    dataset_name = request.form.get('dataset_name', 'New Dataset')
    sanitized_name = ''.join(c if c.isalnum() or c in '-_' else '-' for c in dataset_name)
    sanitized_name = sanitized_name.strip('-_')
    collections = chroma_client.list_collections()
    collection_names = [c.name for c in collections]
    if sanitized_name in collection_names:
        return jsonify({"status": "error", "message": "Dataset name already taken"}), 400
    return orig_upload_documents()
app.view_functions['upload_documents'] = upload_documents_patched

# Add an alias for the update endpoint to support /update
@app.route('/api/datasets/<dataset_name>/update', methods=['PUT'])
def update_dataset_metadata_route_alias(dataset_name):
    return update_dataset_metadata_route(dataset_name)

if __name__ == '__main__':
    app.run(debug=True, port=5000)