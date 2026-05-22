const fs = require('fs');
const path = require('path');

// Ensure directory exists
function ensureDirExists(dirPath) {
  if (!fs.existsSync(dirPath)) {
    fs.mkdirSync(dirPath, { recursive: true });
  }
}

// Copy file
function copyFile(src, dest) {
  ensureDirExists(path.dirname(dest));
  fs.copyFileSync(src, dest);
  console.log(`Copied ${src} -> ${dest}`);
}

// Copy directory recursively
function copyDir(src, dest) {
  // Prevent recursive copying if dest is inside src
  if (path.resolve(dest).startsWith(path.resolve(src) + path.sep)) {
    return;
  }
  ensureDirExists(dest);
  const entries = fs.readdirSync(src, { withFileTypes: true });

  for (let entry of entries) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);

    if (entry.isDirectory()) {
      copyDir(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

// Run build steps
try {
  ensureDirExists('www');
  copyFile('templates/index.html', 'www/index.html');
  copyFile('static/sw.js', 'www/sw.js');
  copyDir('static', 'www/static');
  console.log('Build completed successfully!');
} catch (err) {
  console.error('Build failed:', err);
  process.exit(1);
}
