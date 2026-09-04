import { useEffect, useState } from 'react'
import { useTarangStore } from './state/store'
import { fetchSources, fetchMetadata } from './api/client'
import { buildTimeStepLabels } from './api/time'
import { prewarmIndiaRegion } from './api/prewarm'

import { ForecasterConsole } from './modes/ForecasterConsole'
import { ExplorerMode } from './modes/ExplorerMode'
import { VolumeWorkspace } from './modes/VolumeWorkspace'

import { HomeOverlay } from './components/HomeOverlay'
import GetStarted from './pages/GetStarted'

import './index.css'

/**
 * App Shell
 *
 * Normal TARANG flow:
 * HomeOverlay
 *     ↓
 * ExplorerMode / ForecasterConsole / VolumeWorkspace
 *
 * Beginner flow:
 * HomeOverlay
 *     ↓
 * GET STARTED
 *     ↓
 * GetStarted page   (now an overlay on top of the scene, same as HomeOverlay,
 *                     so the live 3D globe stays visible behind it)
 *     ↓
 * START EXPLORING
 *     ↓
 * Existing TARANG visualization
 */

export default function App() {
  // Controls whether the beginner guide is visible
  const [showGetStarted, setShowGetStarted] = useState(false)

  const uiMode = useTarangStore(s => s.uiMode)
  const renderMode = useTarangStore(s => s.renderMode)
  const activeSourceId = useTarangStore(s => s.activeSourceId)
  const setShowHomeOverlay = useTarangStore(s => s.setShowHomeOverlay)

  const setSources = useTarangStore(s => s.setSources)
  const setDepthLevels = useTarangStore(s => s.setDepthLevels)
  const setTimeSteps = useTarangStore(s => s.setTimeSteps)
  const setActiveVar = useTarangStore(s => s.setActiveVar)
  const setVariableMeta = useTarangStore(s => s.setVariableMeta)
  const setLoading = useTarangStore(s => s.setLoading)
  const setError = useTarangStore(s => s.setError)

  // ---------------------------------------------------------
  // Bootstrap
  // ---------------------------------------------------------

  useEffect(() => {
    const controller = new AbortController()

    async function bootstrap() {
      setLoading(true)

      try {
        // 1. Load available data sources
        const sources = await fetchSources(controller.signal)

        if (sources.length > 0) {
          setSources(sources)
        } else if (
          useTarangStore.getState().sources.length === 0
        ) {
          throw new Error(
            'backend returned no data sources (starting up?)'
          )
        }

        // 2. Load metadata
        const meta = await fetchMetadata(
          activeSourceId,
          controller.signal
        )

        setDepthLevels(meta.depth_levels)

        setTimeSteps(
          buildTimeStepLabels(
            meta.time_range?.start,
            meta.time_range?.end,
            meta.time_range?.steps
          )
        )

        // Feed variable dropdown
        setVariableMeta(
          meta.available_variables,
          meta.cf_metadata
        )

        const initialVar = meta.available_variables[0]

        if (initialVar) {
          setActiveVar(initialVar)

          // India is the default scope
          if (
            useTarangStore.getState().viewScope === 'india'
          ) {
            prewarmIndiaRegion(
              activeSourceId,
              initialVar
            )
          }
        }
      } catch (e: unknown) {
        if ((e as Error).name !== 'AbortError') {
          setError(
            `Failed to connect to backend: ${
              (e as Error).message
            }`
          )
        }
      } finally {
        setLoading(false)
      }
    }

    bootstrap()

    return () => controller.abort()
  }, [activeSourceId])

  // ---------------------------------------------------------
  // Self healer
  // ---------------------------------------------------------

  useEffect(() => {
    let running = false

    async function heal() {
      if (running) return

      const st = useTarangStore.getState()

      const stale =
        st.sources.length === 0 ||
        !st.sources.some(
          s => s.id === st.activeSourceId
        )

      if (!stale) return

      running = true

      try {
        const sources = await fetchSources()

        if (sources.length > 0) {
          setSources(sources)

          const currentState =
            useTarangStore.getState()

          const sourceId =
            sources.some(
              s => s.id === currentState.activeSourceId
            )
              ? currentState.activeSourceId
              : sources[0].id

          if (
            sourceId !==
            useTarangStore.getState().activeSourceId
          ) {
            useTarangStore
              .getState()
              .setActiveSource(sourceId)
          }

          const meta =
            await fetchMetadata(sourceId)

          setDepthLevels(
            meta.depth_levels
          )

          setTimeSteps(
            buildTimeStepLabels(
              meta.time_range?.start,
              meta.time_range?.end,
              meta.time_range?.steps
            )
          )

          setVariableMeta(
            meta.available_variables,
            meta.cf_metadata
          )

          if (meta.available_variables[0]) {
            setActiveVar(
              meta.available_variables[0]
            )
          }

          setError(null)
        }
      } catch {
        // Backend still unavailable.
        // The next healing cycle will try again.
      } finally {
        running = false
      }
    }

    const id = setInterval(
      heal,
      4000
    )

    window.addEventListener(
      'focus',
      heal
    )

    heal()

    return () => {
      clearInterval(id)

      window.removeEventListener(
        'focus',
        heal
      )
    }
  }, [])

  // ---------------------------------------------------------
  // NORMAL TARANG APPLICATION
  // (scene always mounted; HomeOverlay and GetStarted are both
  //  transparent overlays that sit on top of it)
  // ---------------------------------------------------------

  return (
    <div
      id="tarang-root"
      style={{
        width: '100vw',
        height: '100vh',
        overflow: 'hidden'
      }}
    >
      {showGetStarted ? (
        <GetStarted
          onBack={() => setShowGetStarted(false)}
          onStartExploring={() => {
            setShowGetStarted(false)
            setShowHomeOverlay(false)
          }}
        />
      ) : (
        <HomeOverlay
          onGetStarted={() => setShowGetStarted(true)}
        />
      )}

      {renderMode === 'cube' ? (
        <VolumeWorkspace />
      ) : uiMode === 'console' ? (
        <ForecasterConsole />
      ) : (
        <ExplorerMode />
      )}
    </div>
  )
}