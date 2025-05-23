# Legal Sanctions RAG UI Implementation Tasks

This document outlines the tasks for rebuilding the Legal Sanctions RAG application UI. The plan is structured to guide integration into a new, secure codebase.

## Overview
The Legal Sanctions RAG application is a specialized legal expert chatbot system focusing on international sanctions regulations. It allows users to interact with an AI assistant enhanced with retrieval augmented generation that provides analysis and answers with specific legal provisions and sources.

## Core Components

### 1. Main Layout Structure
- [x] Implement a responsive layout with sidebar and main content area
- [x] Create collapsible sidebar for mobile responsiveness
- [x] Add logo and application title in the sidebar header

### 2. Chat Interface
- [x] Create a clean chat message container
- [x] Implement user and assistant message styling 
- [x] Add chat input form with submit button
- [x] Design welcome message for new chats
- [x] Implement auto-scrolling for new messages
- [x] Add streaming response capability with SSE (Server-Sent Events)
- [x] Create thinking/loading animation for responses in progress
- [x] Implement chat continuation and "last active chat" restoration

### 3. Structured Response Formatting
- [x] Format AI responses with sections:
  - Sources section with citation of relevant documents
  - Analysis section with step-by-step reasoning
  - Applicable Provisions section listing relevant legal sections
  - Conclusion section with the final answer
- [x] Style each section distinctively with proper headings
- [x] Format bulleted lists and other markdown elements
- [x] Add support for syntax highlighting in code blocks
- [x] Create interface for exploring context sources used in responses

### 4. Chat Management
- [x] Implement folder organization system for chats
- [x] Create folder creation/renaming/deletion functionality
- [x] Add chat creation/deletion capabilities
- [x] Create rename chat functionality
- [x] Implement drag-and-drop for organizing chats into folders
- [x] Add context menu for chat management options
- [x] Implement chat history persistence
- [x] Create auto-naming of chats based on content

### 5. RAG Model Management
- [x] Create RAG model selection UI
- [x] Implement RAG model creation
- [x] Add RAG model deletion capabilities
- [x] Create document counter badge for each RAG model
- [x] Add current RAG model indicator in the chat interface
- [x] Implement RAG model source exploration interface
- [x] Add RAG model assignment for uploaded documents

### 6. Document Upload
- [x] Create document upload modal
- [x] Implement file type validation (PDF, TXT)
- [x] Add drag-and-drop upload capability
- [x] Create upload progress indicator
- [x] Implement file size validation
- [x] Add dataset assignment for uploaded documents
- [x] Implement error handling for failed uploads
- [x] Create secure document upload with encryption UI indicators

### 7. Feedback System
- [x] Create feedback submission form
- [x] Implement feedback rating system
- [x] Add feedback categories (Helpful, Not Helpful, Inaccurate)
- [x] Create toast notifications for feedback submission status
- [x] Implement message-specific feedback options
- [x] Add feedback history viewing interface

### 8. User Interface Enhancements
- [x] Implement custom scrollbar styling
- [x] Add toast notifications for system messages
- [x] Create hover effects for interactive elements
- [x] Implement keyboard shortcuts for common actions
- [x] Add animation for transitions and interactions
- [x] Create responsive design for mobile and desktop
- [x] Implement error visualization and recovery interfaces

### 9. Security Features
- [x] Implement user authentication UI (login/registration forms)
- [x] Create user profile view and management interface
- [x] Add secure document viewer with access token handling
- [x] Create permission-based UI elements that adapt to user roles
- [x] Implement MFA verification and setup screens
- [x] Add password change functionality

### 10. Credit System
- [x] Create credit balance display in user interface
- [x] Implement credit transaction history view
- [x] Add credit package purchasing interface
- [x] Create usage cost indicators for document processing
- [x] Implement low credit warnings and notifications

## Implementation Notes

### React Components Structure
Organize the application with the following component structure:
```
App/
├── Layout/
│   ├── Sidebar
│   └── MainContent
├── Chat/
│   ├── ChatContainer
│   ├── ChatMessage
│   ├── ChatInput
│   └── ThinkingAnimation
├── Folders/
│   ├── FolderList
│   ├── FolderItem
│   └── ChatItem
├── Datasets/
│   ├── DatasetModal
│   ├── DatasetList
│   └── DatasetItem
├── Documents/
│   ├── DocumentUpload
│   ├── FileDropzone
│   └── UploadProgress
├── Feedback/
│   ├── FeedbackModal
│   └── RatingSelector
└── Common/
    ├── ToastNotification
    ├── ContextMenu
    └── Modal
```

### Key API Endpoints
```
// Chat Management
GET /api/chats - List all chats
POST /api/chats - Create a new chat
GET /api/chats/{id} - Get a specific chat
PUT /api/chats/{id} - Update a chat
DELETE /api/chats/{id} - Delete a chat
POST /api/chats/{id}/messages - Add a message to chat
POST /api/chats/{id}/messages/stream - Stream responses
POST /api/save-last-chat - Save last active chat in session

// Folder Management
GET /api/folders - List all folders
POST /api/folders - Create a new folder
DELETE /api/folders/{id} - Delete a folder
POST /api/chats/{id}/move - Move a chat to a folder

// Dataset Management
GET /api/datasets - Get available datasets
DELETE /api/datasets/{name} - Delete a dataset
POST /api/upload-documents - Upload documents to create dataset
POST /api/process-documents - Process existing documents

// Document Management
POST /api/secure-documents/upload - Upload encrypted documents
GET /api/secure-documents/{document_id} - Get secure document access token
GET /api/secure-documents/stream/{access_token} - Stream secure document
DELETE /api/secure-documents/{document_id} - Delete secure document

// Feedback
POST /api/feedback - Submit general feedback
POST /api/user-feedback - Submit structured user feedback

// Authentication
POST /api/auth/register - Register new user
POST /api/auth/login - User login
POST /api/auth/verify-mfa - Verify MFA code
POST /api/auth/refresh - Refresh authentication token
POST /api/auth/logout - User logout

// User Management
GET /api/users/me - Get current user profile
PUT /api/users/me - Update user profile
POST /api/users/change-password - Change user password

// Credit System
GET /api/credits/balance - Get user credit balance
GET /api/credits/history - Get credit transaction history
GET /api/credits/packages - Get available credit packages
POST /api/credits/purchase - Purchase credits

// RAG Model Management
GET /api/ragmodels - Get available RAG models
DELETE /api/ragmodels/{name} - Delete a RAG model
POST /api/upload-documents - Upload documents to create RAG model
```

### CSS Styling Notes
- Use a CSS framework like Tailwind CSS for responsive design
- Implement custom scrollbar styling for better user experience
- Style code blocks with syntax highlighting
- Use animation for loading states and transitions
- Provide clear visual distinction between user and AI messages
- Style structured AI responses with clear section demarcation
- Create toast notifications for user feedback

### JavaScript Features
- Implement SSE for streaming responses
- Use Markdown parsing for formatted responses
- Implement drag-and-drop functionality for chat organization
- Add context menu for additional actions
- Create toast notification system for user feedback
- Build persistent state management for chat history
- Implement folder and chat hierarchy management

## Integration Steps

1. Create the base layout structure with sidebar and main content area
2. Implement core chat functionality with message display and input
3. Add folder and chat management capabilities
4. Implement RAG model selection and document upload features
5. Create structured response formatting
6. Add streaming capability for real-time responses
7. Implement feedback system
8. Add UI enhancements and animations
9. Integrate user authentication and profile management 
10. Implement secure document handling with encryption
11. Create credit system UI components
12. Add role-based permission controls to UI elements
13. Implement context source exploration features
14. Add MFA verification flows
15. Create comprehensive error handling and recovery UIs
16. Test thoroughly across different devices and screen sizes