class ImageColorizerApp {
    constructor() {
        this.selectedFiles = [];
        this.maxFiles = 25;  
        this.maxFileSize = 1024 * 1024; //
        this.rateLimitWindow = 60000; 
        this.uploadCount = 0;
        this.lastResetTime = Date.now();
        this.perRequestMax = 5; 
        
        this.initializeFingerprinting();
        this.initializeEventListeners();
        this.generateSessionToken();
        this.updateRateLimitDisplay();
        this.testBackendConnection();
        this.init();
    }
    async init() {
    await this.loadConfig();           
    await this.testBackendConnection();
  }
    
    async loadConfig() {
    const response = await fetch("/config"); 
    const config = await response.json();
    this.backendUrl = config.backendUrl;
}

    async testBackendConnection() {
        await new Promise(resolve => setTimeout(resolve, 2000));
        try {
            const response = await fetch(`${this.backendUrl}/health`);
            if (response.ok) {
                const health = await response.json();
                console.log(' Backend connected:', health);
                
            } else {
                throw new Error('Backend health check failed');
            }
        } catch (error) {
            console.error(' Backend connection failed:', error);
            this.showMessage('Warning: Backend connection failed. Please check if the server is running.', 'warning');
        }
    }

    initializeFingerprinting() {
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        ctx.textBaseline = 'top';
        ctx.font = '14px Arial';
        ctx.fillText('Browser fingerprint', 2, 2);
        let fingerprint;
        if (typeof CryptoJS !== 'undefined') {
            fingerprint = CryptoJS.SHA256(
                navigator.userAgent +
                navigator.language +
                screen.width + screen.height +
                new Date().getTimezoneOffset() +
                canvas.toDataURL()
            ).toString();
        } else {
            fingerprint = btoa(
                navigator.userAgent +
                navigator.language +
                screen.width + screen.height +
                new Date().getTimezoneOffset()
            ).replace(/[^a-zA-Z0-9]/g, '').substring(0, 32);
        }
        
        document.getElementById('browserFingerprint').value = fingerprint;
    }

    generateSessionToken() {
  const k = 'colorizer:session';
  let token = localStorage.getItem(k);
  if (!token) {
    token = (crypto?.randomUUID?.() ??
      'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random()*16|0, v = c==='x' ? r : (r&0x3|0x8);
        return v.toString(16);
      }));
    localStorage.setItem(k, token);
  }
  document.getElementById('sessionToken').value = token;
}
    initializeEventListeners() {
        const uploadZone = document.getElementById('uploadZone');
        const fileInput = document.getElementById('fileInput');
        const processBtn = document.getElementById('processBtn');

        this.isFileDialogOpen = false;

        uploadZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadZone.classList.add('dragover');
        });

        uploadZone.addEventListener('dragleave', () => {
            uploadZone.classList.remove('dragover');
        });

        uploadZone.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadZone.classList.remove('dragover');
            this.handleFiles(e.dataTransfer.files);
        });

        uploadZone.addEventListener('click', (e) => {
            if (!this.isFileDialogOpen && (e.target === uploadZone || e.target.closest('#uploadZone') === uploadZone)) {
                this.isFileDialogOpen = true;
                fileInput.click();
            }
        });

        fileInput.addEventListener('change', (e) => {
            this.isFileDialogOpen = false;
            if (e.target.files.length > 0) {
                this.handleFiles(e.target.files);
            }
        });

        fileInput.addEventListener('focus', () => {
            this.isFileDialogOpen = false;
        });

        window.addEventListener('focus', () => {
            this.isFileDialogOpen = false;
        });

        processBtn.addEventListener('click', () => {
            this.processImages();
        });

        document.addEventListener('dragover', (e) => e.preventDefault());
        document.addEventListener('drop', (e) => e.preventDefault());
    }

    updateRateLimitDisplay() {
        const now = Date.now();
        if (now - this.lastResetTime > this.rateLimitWindow) {
            this.uploadCount = 0;
            this.lastResetTime = now;
        }

        const remaining = Math.max(0, this.perRequestMax - this.uploadCount);
        document.getElementById('rateLimitCounter').textContent = `${remaining}/${this.perRequestMax}`;
    }

    async preUploadCheck(newFileCount) {
        try {
            const fp = document.getElementById('browserFingerprint').value;
            const response = await fetch(`${this.backendUrl}/upload/check`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Client-Fingerprint': fp,   
                },
                body: JSON.stringify({
                    currentFileCount: this.selectedFiles.length,
                    newFileCount: newFileCount,
                    totalFileCount: this.selectedFiles.length + newFileCount,
                    sessionToken: document.getElementById('sessionToken').value,
                    fingerprint: fp
                })
            });
            
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
                throw new Error(errorData.detail || 'Pre-upload check failed');
            }
            
            return await response.json();
        } catch (error) {
            console.error('Pre-upload check failed:', error);
            throw error;
        }
    }

    showMessage(message, type = 'info') {
        const messagesDiv = document.getElementById('messages');
        const messageEl = document.createElement('div');
        messageEl.className = type;
        messageEl.textContent = message;
        messagesDiv.appendChild(messageEl);
        
        setTimeout(() => {
            messageEl.remove();
        }, 5000);
    }

    validateImage(file) {
        const errors = [];
        
        if (file.size > this.maxFileSize) {
            errors.push(`File ${file.name} exceeds 1MB limit`);
        }

        if (!file.type.startsWith('image/')) {
            errors.push(`File ${file.name} is not a valid image`);
        }

        return errors;
    }

    async checkImageQuality(file) {
        return new Promise((resolve) => {
            const img = new Image();
            const canvas = document.createElement('canvas');
            const ctx = canvas.getContext('2d');

            img.onload = () => {
                canvas.width = img.width;
                canvas.height = img.height;
                ctx.drawImage(img, 0, 0);

                const warnings = [];
                
                if (img.width < 256 || img.height < 256) {
                    warnings.push(`${file.name}: Low resolution detected (${img.width}x${img.height}). Results may be suboptimal.`);
                }

                const imageData = ctx.getImageData(0, 0, Math.min(100, img.width), Math.min(100, img.height));
                let colorPixels = 0;
                
                for (let i = 0; i < imageData.data.length; i += 4) {
                    const r = imageData.data[i];
                    const g = imageData.data[i + 1];
                    const b = imageData.data[i + 2];
                    
                    if (!(r === g && g === b)) {
                        colorPixels++;
                    }
                }
                
                const colorRatio = colorPixels / (imageData.data.length / 4);
                if (colorRatio > 0.1) {
                    warnings.push(`${file.name}: Image appears to already contain color information.`);
                }

                resolve(warnings);
            };

            img.onerror = () => resolve([`${file.name}: Could not analyze image quality`]);
            img.src = URL.createObjectURL(file);
        });
    }

    async handleFiles(files) {
        const honeyPot = document.getElementById('honeyPot');
        if (honeyPot && honeyPot.value) {
            this.showMessage('Security check failed', 'error');
            return;
        }

        const fileArray = Array.from(files);
        
        this.updateRateLimitDisplay();
        if (this.uploadCount + fileArray.length > this.perRequestMax) {
            this.showMessage(`Rate limit would be exceeded. You can only upload ${this.rateLimit - this.uploadCount} more file(s).`, 'error');
            return;
        }

        if (this.selectedFiles.length + fileArray.length > this.perRequestMax) {
            this.showMessage(`Cannot exceed ${this.maxFiles} files total`, 'error');
            return;
        }

        let allErrors = [];
        let allWarnings = [];

        for (const file of fileArray) {
            const errors = this.validateImage(file);
            allErrors = allErrors.concat(errors);
            
            if (errors.length === 0) {
                const warnings = await this.checkImageQuality(file);
                allWarnings = allWarnings.concat(warnings);
            }
        }

        if (allErrors.length > 0) {
            allErrors.forEach(error => this.showMessage(error, 'error'));
            return;
        }

        if (allWarnings.length > 0) {
            allWarnings.forEach(warning => this.showMessage(warning, 'warning'));
        }

        try {
            const checkResult = await this.preUploadCheck(fileArray.length);
            if (!checkResult.allowed) {
                this.showMessage('Upload not allowed at this time', 'error');
                return;
            }
        } catch (error) {
            this.showMessage(`Upload check failed: ${error.message}`, 'error');
            return;
        }

        this.selectedFiles = this.selectedFiles.concat(fileArray);
        this.uploadCount += fileArray.length;
        this.updateRateLimitDisplay();
        this.renderFilePreview();
        this.updateProcessButton();

        const fileInput = document.getElementById('fileInput');
        fileInput.value = '';

        this.showMessage(`Added ${fileArray.length} file(s) successfully`, 'success');
    }

    renderFilePreview() {
        const previewDiv = document.getElementById('filePreview');
        previewDiv.innerHTML = '';
        this.selectedFiles.forEach((file, index) => {
            if (!file._previewUrl) file._previewUrl = URL.createObjectURL(file); 

            const fileItem = document.createElement('div');
            fileItem.className = 'file-item';

            const img = document.createElement('img');
            img.className = 'file-image';
            img.src = file._previewUrl;     
            img.alt = file.name;

            const fileInfo = document.createElement('div');
            fileInfo.className = 'file-info';
            
            const fileName = document.createElement('div');
            fileName.className = 'file-name';
            fileName.textContent = file.name;
            
            const fileSize = document.createElement('div');
            fileSize.className = 'file-size';
            fileSize.textContent = this.formatFileSize(file.size);

            const removeBtn = document.createElement('button');
            removeBtn.className = 'remove-btn';
            removeBtn.textContent = '×';
            removeBtn.onclick = () => this.removeFile(index);

            fileInfo.appendChild(fileName);
            fileInfo.appendChild(fileSize);
            fileItem.appendChild(img);
            fileItem.appendChild(fileInfo);
            fileItem.appendChild(removeBtn);
            previewDiv.appendChild(fileItem);
        });
    }

    removeFile(index) {
        const f = this.selectedFiles[index];
        if (f && f._previewUrl) {
            URL.revokeObjectURL(f._previewUrl);
            delete f._previewUrl;
        }
        this.selectedFiles.splice(index, 1);
        this.uploadCount = Math.max(0, this.uploadCount - 1);
        this.updateRateLimitDisplay();
        this.renderFilePreview();
        this.updateProcessButton();
        }

    updateProcessButton() {
        const processBtn = document.getElementById('processBtn');
        processBtn.disabled = this.selectedFiles.length === 0;
        processBtn.textContent = this.selectedFiles.length > 0 
            ? ` Colorize ${this.selectedFiles.length} Image${this.selectedFiles.length > 1 ? 's' : ''}`
            : ' Colorize Images';
    }

    formatFileSize(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }
    
    async processImages() {
        if (this.selectedFiles.length === 0) return;

        const loadingDiv = document.getElementById('loading');
        const processBtn = document.getElementById('processBtn');
        const fp = document.getElementById('browserFingerprint').value;
        const token = document.getElementById('sessionToken').value;

      
        const chunks = [];
        for (let i = 0; i < this.selectedFiles.length; i += this.perRequestMax) {
            chunks.push(this.selectedFiles.slice(i, i + this.perRequestMax));
        }

        loadingDiv.style.display = 'block';
        processBtn.disabled = true;
        processBtn.textContent = 'Processing...';

        try {
            for (let i = 0; i < chunks.length; i++) {
                const chunk = chunks[i];
                const formData = new FormData();
                chunk.forEach(f => formData.append('files', f));
                formData.append('sessionToken', token);
                formData.append('fingerprint', fp);

                const resp = await fetch(`${this.backendUrl}/api/colorize`, {
                    method: 'POST',
                    headers: { 'X-Client-Fingerprint': fp },
                    body: formData
                });

                if (resp.status === 429) {
                    const retry = resp.headers.get('Retry-After');
                    throw new Error(`Rate limit hit. Try again in ${retry ? `${retry}s` : 'a minute'}.`);
                }
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({ detail: 'Unknown error' }));
                    throw new Error(err.detail || `HTTP ${resp.status}`);
                }

                const result = await resp.json();
                this.showMessage(`Batch ${i + 1}/${chunks.length}: ${result.message}`, 'success');
            }

            // Refresh gallery once at the end
            await this.refreshResults();
            this.selectedFiles = [];
            this.renderFilePreview();
            this.updateProcessButton();

        } catch (error) {
            console.error('Processing error:', error);
            this.showMessage('Processing failed: ' + error.message, 'error');
        } finally {
            loadingDiv.style.display = 'none';
            processBtn.disabled = false;
            processBtn.textContent = this.selectedFiles.length > 0
                ? `Colorize ${this.selectedFiles.length} Image${this.selectedFiles.length > 1 ? 's' : ''}`
                : ' Colorize Images';
        }
    }

    async refreshResults() {
        const token = document.getElementById('sessionToken').value;
        const resp  = await fetch(`${this.backendUrl}/api/results/${token}`);
        if (!resp.ok) {
            console.error("Failed to fetch session results:", resp.status);
            return;
        }
        const data = await resp.json();
      
        const urls = data.results.map(r => r.url);
        console.log(" refreshResults got URLs:", urls);
        this.displayResults(urls);
    }

    displayResults(urls) {
        const container = document.getElementById("results");
        
        container.innerHTML = "";

        urls.forEach((relativeUrl, idx) => {
            const fileName = relativeUrl.split("/").pop();
            const fullUrl  = `${this.backendUrl}${relativeUrl}`;

            const card = document.createElement("div");
            card.className = "file-item";

            const img = document.createElement("img");
            img.className    = "file-image";
            img.src          = fullUrl;
            img.alt          = fileName;
            img.style.objectFit   = "contain";  
            img.style.background   = "#102542";   
            card.appendChild(img);

            const info = document.createElement("div");
            info.className = "file-info";

            const nameDiv = document.createElement("div");
            nameDiv.className = "file-name";
            nameDiv.textContent = fileName;
            info.appendChild(nameDiv);

            const dl = document.createElement("a");
            dl.className    = "download-btn";
            dl.href         = fullUrl;
            dl.download     = fileName;
            dl.textContent  = "⬇️ Download";
            info.appendChild(dl);

            card.appendChild(info);

            container.appendChild(card);
        });

        console.log("Colorized images displayed:", urls);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    new ImageColorizerApp();
});