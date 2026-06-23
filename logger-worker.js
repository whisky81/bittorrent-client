import chalk from 'chalk';
import cliProgress from 'cli-progress';

function timestamp() {
  return chalk.gray(new Date().toTimeString().slice(0, 8));
}

function toNum(v) {
  const n = parseFloat(v);
  return isFinite(n) ? n : 0;
}

function fmt2(v) {
  return toNum(v).toFixed(2);
}

const multiBar = new cliProgress.MultiBar(
  {
    hideCursor: true,
    clearOnComplete: false,
    stopOnComplete: false,
    forceRedraw: false,
  },
  cliProgress.Presets.rect
);

const downloadBar = multiBar.create(
  100,
  0,
  {
    downloaded: '0.00',
    uploaded: '0.00',
    dlSpeed: '0.00',
    ulSpeed: '0.00',
    peers: 0,
  },
  {
    format:
      chalk.cyan(' DL  [{bar}]') +
      ' {percentage}%  ' +
      chalk.cyan('↓') +
      ' {downloaded}MB ({dlSpeed} MB/s)  ' +
      chalk.magenta('↑') +
      ' {uploaded}MB ({ulSpeed} MB/s)  ' +
      chalk.yellow('⇄') +
      ' {peers} peers',
    barsize: 28,
  }
);

const pieceBar = multiBar.create(
  1,
  0,
  {
    done: 0,
    total: 0,
  },
  {
    format:
      chalk.green(' PCS [{bar}]') + ' {value}/{total} pieces  ' + chalk.gray('({percentage}%)'),
    barsize: 28,
  }
);

let isBarStarted = false;
let prevDownloaded = 0;
let prevUploaded = 0;
let prevTimestamp = Date.now();

function logSecurely(message) {
  if (isBarStarted) {
    multiBar.log(message + '\n');
  } else {
    console.log(message);
  }
}

process.on('message', (packet) => {
  const { event, data } = packet;

  switch (event) {
    case 'logging':
      logSecurely(`${timestamp()} ${chalk.red('⚠')}  ${data.message}`);
      break;

    case 'piece':
      logSecurely(
        `${timestamp()} ${chalk.green('✔ piece')}  ` +
          `#${chalk.white(String(data.pieceIndex).padStart(5, '0'))}  ` +
          chalk.gray(`remaining: ${data.remainingPieces}`)
      );
      break;

    case 'failed':
      logSecurely(
        `${timestamp()} ${chalk.red('✘ failed')} ` +
          `#${chalk.white(String(data.pieceIndex).padStart(5, '0'))}`
      );
      break;

    case 'peer:add':
      if (!data.ip || !data.port) break;
      logSecurely(
        `${timestamp()} ${chalk.greenBright('+ peer')}  ` +
          `${data.ip}:${data.port}  ` +
          chalk.gray(data.peerId ?? '—')
      );
      break;

    case 'peer:drop':
      if (!data.ip || !data.port) break;
      logSecurely(
        `${timestamp()} ${chalk.redBright('- peer')}  ` +
          `${data.ip}:${data.port}  ` +
          chalk.gray(data.peerId ?? '—')
      );
      break;

    case 'progress': {
      const s = data.stats;
      if (!isBarStarted) {
        const total = s.totalPieces ?? 1;
        pieceBar.setTotal(total);
        downloadBar.start(100, 0);
        pieceBar.start(total, 0);
        isBarStarted = true;
        prevTimestamp = Date.now();
      }
      const dlMB = toNum(s.downloadedMB);
      const ulMB = toNum(s.uploadedMB);
      const progress = toNum(s.progress);
      const now = Date.now();
      const elapsed = (now - prevTimestamp) / 1000 || 1;
      const dlSpeed = Math.max(0, (dlMB - prevDownloaded) / elapsed).toFixed(2);
      const ulSpeed = Math.max(0, (ulMB - prevUploaded) / elapsed).toFixed(2);
      prevDownloaded = dlMB;
      prevUploaded = ulMB;
      prevTimestamp = now;
      downloadBar.update(Math.floor(progress), {
        downloaded: fmt2(dlMB),
        uploaded: fmt2(ulMB),
        dlSpeed,
        ulSpeed,
        peers: toNum(s.noOfCPeer),
      });
      if (s.totalPieces != null && s.donePieces != null) {
        pieceBar.update(s.donePieces, {
          done: s.donePieces,
          total: s.totalPieces,
        });
      }
      break;
    }
    case 'done':
      if (isBarStarted) {
        downloadBar.update(100);
        multiBar.stop();
      }
      console.log(chalk.green('\n ✔  Download complete!\n'));
      break;
    case 'shutdown':
      if (isBarStarted) {
        multiBar.stop();
      }
      console.log(chalk.yellow(`\n ⏱  Elapsed: ${data.elapsed}`));
      console.log(chalk.gray(JSON.stringify(data.stats, null, 2)));
      process.exit(0);
      break;
    default:
      logSecurely(`${timestamp()} ${chalk.gray('[?]')} ${JSON.stringify(data)}`);
  }
});
