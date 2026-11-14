import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { IFCLoader } from 'web-ifc-three/IFCLoader.js';

export function openIfcFile(file) {
  document.dispatchEvent(new CustomEvent('kraftsync:open-ifc', { detail: file }));
}

export function initBimViewer() {
  const stage = document.getElementById('bim-stage');
  const canvas = document.getElementById('bim-canvas');
  const input = document.getElementById('ifc-file');
  const status = document.getElementById('bim-status');
  const sectionToggle = document.getElementById('bim-interior');
  const sectionSlider = document.getElementById('bim-section');
  const sectionValue = document.getElementById('bim-section-value');
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x202923);
  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000000);
  camera.up.set(0, 0, 1);
  camera.position.set(20, -25, 20);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x677267, 0.65));
  const keyLight = new THREE.DirectionalLight(0xffffff, 0.8);
  keyLight.position.set(20, -30, 50);
  scene.add(keyLight);

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.9;
  renderer.localClippingEnabled = true;
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.screenSpacePanning = true;
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.rotateSpeed = 0.45;
  controls.zoomSpeed = 0.55;
  controls.panSpeed = 0.45;
  controls.minPolarAngle = 0.05;
  controls.maxPolarAngle = Math.PI - 0.05;
  const loader = new IFCLoader();
  loader.ifcManager.setWasmPath('/ifc-wasm/');
  let model;
  let modelBounds;
  const sectionPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);

  function render() {
    renderer.render(scene, camera);
  }
  function resize() {
    const width = stage.clientWidth;
    const height = stage.clientHeight;
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  function fitCamera(saveView = true) {
    if (!model || !modelBounds) return;
    const sphere = modelBounds.getBoundingSphere(new THREE.Sphere());
    const radius = Math.max(sphere.radius, 0.1);
    controls.target.copy(sphere.center);
    camera.near = Math.max(radius / 1000, 0.01);
    camera.far = radius * 100;
    const verticalFov = THREE.MathUtils.degToRad(camera.fov);
    const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * camera.aspect);
    const distance = radius / Math.sin(Math.min(verticalFov, horizontalFov) / 2) * 1.08;
    const direction = new THREE.Vector3(1.8, -2.2, 2).normalize();
    camera.position.copy(sphere.center).addScaledVector(direction, distance);
    camera.updateProjectionMatrix();
    controls.minDistance = radius * 0.02;
    controls.maxDistance = radius * 30;
    controls.update();
    if (saveView) controls.saveState();
  }
  function applySection() {
    if (!model || !modelBounds) return;
    const height = Number(sectionSlider.value);
    sectionPlane.constant = -height;
    const materials = Array.isArray(model.material) ? model.material : [model.material];
    for (const material of materials) {
      material.clippingPlanes = sectionToggle.checked ? [sectionPlane] : [];
      material.needsUpdate = true;
    }
    sectionValue.textContent = height.toFixed(2);
  }
  function showFullBuilding() {
    sectionToggle.checked = false;
    sectionSlider.disabled = true;
    applySection();
  }
  function rotateCamera(direction) {
    if (!model) return;
    const step = 0.12;
    const offset = camera.position.clone().sub(controls.target);
    const worldUp = new THREE.Vector3(0, 0, 1);
    if (direction === 'left' || direction === 'right') {
      offset.applyAxisAngle(worldUp, direction === 'left' ? step : -step);
    } else {
      const viewDirection = camera.getWorldDirection(new THREE.Vector3());
      const right = viewDirection.cross(worldUp).normalize();
      if (right.lengthSq()) offset.applyAxisAngle(right, direction === 'up' ? step : -step);
    }
    camera.position.copy(controls.target).add(offset);
    controls.update();
  }
  function zoomCamera(direction) {
    if (!model) return;
    const offset = camera.position.clone().sub(controls.target);
    const distance = THREE.MathUtils.clamp(offset.length() * (direction === 'in' ? 0.88 : 1.14),
      controls.minDistance, controls.maxDistance);
    camera.position.copy(controls.target).add(offset.setLength(distance));
    controls.update();
  }
  function showStatus(message, state = 'info') {
    status.textContent = message;
    status.dataset.state = state;
  }

  renderer.setAnimationLoop(() => {
    controls.update();
    render();
  });
  new ResizeObserver(resize).observe(stage);
  document.getElementById('bim-fit').addEventListener('click', () => controls.reset());
  document.querySelectorAll('[data-bim-rotate]').forEach(button => {
    button.addEventListener('click', () => rotateCamera(button.dataset.bimRotate));
  });
  document.querySelectorAll('[data-bim-zoom]').forEach(button => {
    button.addEventListener('click', () => zoomCamera(button.dataset.bimZoom));
  });
  sectionToggle.addEventListener('change', () => {
    sectionSlider.disabled = !sectionToggle.checked;
    applySection();
  });
  sectionSlider.addEventListener('input', applySection);
  document.getElementById('bim-full').addEventListener('click', showFullBuilding);
  async function loadFile(file) {
    input.disabled = true;
    if (!file.name.toLowerCase().endsWith('.ifc')) {
      showStatus('Select a file with the .ifc extension.', 'error');
      input.disabled = false;
      input.value = '';
      return;
    }
    showStatus('Loading ' + file.name + '…', 'loading');
    try {
      const nextModel = await loader.parse(await file.arrayBuffer());
      const bounds = new THREE.Box3().setFromObject(nextModel);
      if (bounds.isEmpty()) throw new Error('The IFC file contains no renderable building geometry.');
      if (model) {
        model.close(scene);
        scene.remove(model);
      }
      model = nextModel;
      modelBounds = bounds;
      scene.add(model);
      fitCamera();
      const heightRange = modelBounds.max.z - modelBounds.min.z;
      sectionSlider.min = String(modelBounds.min.z);
      sectionSlider.max = String(modelBounds.max.z);
      sectionSlider.step = String(Math.max(heightRange / 200, 0.01));
      sectionSlider.value = String(modelBounds.max.z - heightRange * 0.28);
      sectionSlider.disabled = !sectionToggle.checked;
      applySection();
      showStatus('Loaded ' + file.name, 'success');
    } catch (error) {
      showStatus('Could not load IFC: ' + (error.message || 'Invalid or unsupported IFC file.'), 'error');
    } finally {
      input.disabled = false;
      input.value = '';
    }
  }
  input.addEventListener('change', () => {
    if (input.files?.[0]) void loadFile(input.files[0]);
  });
  document.addEventListener('kraftsync:open-ifc', event => void loadFile(event.detail));
  resize();
}