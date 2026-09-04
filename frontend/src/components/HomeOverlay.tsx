import React, { useState, useEffect } from 'react'
import { useTarangStore } from '../state/store'
import { useT } from '../i18n/useT'
import { LANGUAGES } from '../i18n/translations'

interface HomeOverlayProps {
  onGetStarted?: () => void
}

// ─────────────────────────────────────────────
// Icons
// ─────────────────────────────────────────────

const IconGlobe = () => (
  <svg
    width="26"
    height="26"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="12" cy="12" r="10" />
    <line x1="2" y1="12" x2="22" y2="12" />
    <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
  </svg>
)

const IconLayers = () => (
  <svg
    width="26"
    height="26"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <polygon points="12 2 2 7 12 12 22 7 12 2" />
    <polyline points="2 12 12 17 22 12" />
    <polyline points="2 17 12 22 22 17" />
  </svg>
)

const IconSliders = () => (
  <svg
    width="26"
    height="26"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <line x1="4" y1="21" x2="4" y2="14" />
    <line x1="4" y1="10" x2="4" y2="3" />
    <line x1="12" y1="21" x2="12" y2="12" />
    <line x1="12" y1="8" x2="12" y2="3" />
    <line x1="20" y1="21" x2="20" y2="16" />
    <line x1="20" y1="12" x2="20" y2="3" />
    <line x1="1" y1="14" x2="7" y2="14" />
    <line x1="9" y1="8" x2="15" y2="8" />
    <line x1="17" y1="16" x2="23" y2="16" />
  </svg>
)

const IconWave = () => (
  <svg
    width="120"
    height="36"
    viewBox="0 0 120 36"
    fill="none"
  >
    <path
      d="M2 26C14 8 26 8 38 20C50 32 62 32 74 16C86 2 98 2 110 14"
      stroke="url(#waveGrad)"
      strokeWidth="4"
      strokeLinecap="round"
    />

    <defs>
      <linearGradient
        id="waveGrad"
        x1="0"
        y1="0"
        x2="120"
        y2="0"
      >
        <stop
          offset="0%"
          stopColor="#00d4ff"
          stopOpacity="0.3"
        />

        <stop
          offset="50%"
          stopColor="#00d4ff"
        />

        <stop
          offset="100%"
          stopColor="#1046ff"
          stopOpacity="0.3"
        />
      </linearGradient>
    </defs>
  </svg>
)

const IconChevronDown = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <polyline points="6 9 12 15 18 9" />
  </svg>
)

// ─────────────────────────────────────────────
// FAQ
// ─────────────────────────────────────────────

const FAQ_ITEMS = [
  {
    q: 'Why do we need this at all?',
    a: 'Ocean data today is scattered across Copernicus, HYCOM, INCOIS and buoy feeds in raw NetCDF files only specialists can read. TARANG fuses them into one live 3D view anyone can explore.',
  },
  {
    q: 'Who is this actually useful for?',
    a: 'Fishermen planning safe routes, disaster-management teams tracking cyclones and storm surges, researchers studying eddies and thermal fronts, and coastal planners.',
  },
  {
    q: "What problem does it solve that existing tools don't?",
    a: 'Most ocean portals show flat 2D maps or one variable at a time. TARANG lets you scrub through depth and time together, in 3D, with multiple variables overlaid at once.',
  },
  {
    q: 'What data is it actually built on?',
    a: 'Live and archived feeds from INCOIS, Copernicus Marine Service, HYCOM ocean models, and HF radar — updated on a rolling schedule.',
  },
  {
    q: 'Do I need any technical background to use it?',
    a: 'No. Explorer Mode is point-and-click for anyone. Forecaster Console adds deeper controls for researchers who want them.',
  },
]

const NAV_ITEMS = [
  'EXPLORE',
  'ANALYZE',
  'FORECAST',
]

// ─────────────────────────────────────────────
// Home Overlay
// ─────────────────────────────────────────────

export function HomeOverlay({
  onGetStarted,
}: HomeOverlayProps) {

  const showHomeOverlay =
    useTarangStore(s => s.showHomeOverlay)

  const setShowHomeOverlay =
    useTarangStore(s => s.setShowHomeOverlay)

  const language =
    useTarangStore(s => s.language)

  const setLanguage =
    useTarangStore(s => s.setLanguage)

  const t = useT()

  const [isFadingOut, setIsFadingOut] =
    useState(false)

  const [openFaq, setOpenFaq] =
    useState<number | null>(null)

  // ─────────────────────────────────────────
  // Prevent background scrolling
  // ─────────────────────────────────────────

  useEffect(() => {

    document.body.style.overflow =
      showHomeOverlay
        ? 'hidden'
        : 'auto'

    return () => {
      document.body.style.overflow = 'auto'
    }

  }, [showHomeOverlay])

  // ─────────────────────────────────────────
  // Don't render if home overlay is closed
  // ─────────────────────────────────────────

  if (
    !showHomeOverlay &&
    !isFadingOut
  ) {
    return null
  }

  // ─────────────────────────────────────────
  // ENTER TARANG
  // ─────────────────────────────────────────

  const handleStart = () => {

    setIsFadingOut(true)

    setTimeout(() => {

      setShowHomeOverlay(false)

      setIsFadingOut(false)

    }, 700)
  }

  // ─────────────────────────────────────────
  // Render
  // ─────────────────────────────────────────

  return (

    <div
      style={{
        position: 'absolute',
        top: 0,
        left: 0,

        width: '100vw',
        height: '100vh',

        zIndex: 9999,

        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',

        background: 'transparent',

        opacity:
          isFadingOut
            ? 0
            : 1,

        transform:
          isFadingOut
            ? 'scale(1.05)'
            : 'scale(1)',

        transition:
          'all 0.7s cubic-bezier(0.85, 0, 0.15, 1)',

        pointerEvents:
          isFadingOut
            ? 'none'
            : 'auto',

        color: '#e0f0ff',

        overflowY: 'auto',
        overflowX: 'hidden',
      }}
    >

      {/* ═══════════════════════════════════
          MAIN CONTENT
          ═══════════════════════════════════ */}

      <div
        style={{
          width:
            'min(880px, 92vw)',

          display: 'flex',

          flexDirection:
            'column',

          alignItems:
            'center',

          textAlign:
            'center',

          padding:
            'clamp(28px, 6vh, 64px) 20px 40px',

          gap: '20px',
        }}
      >

        {/* ───────────────────────────────
            Wave mark
        ─────────────────────────────── */}

        <div
          style={{
            animation:
              'slide-up-stagger 1s cubic-bezier(0.16, 1, 0.3, 1) forwards',
          }}
        >
          <IconWave />
        </div>

        {/* ───────────────────────────────
            Logo
        ─────────────────────────────── */}

        <div
          style={{
            animation:
              'slide-up-stagger 1s cubic-bezier(0.16, 1, 0.3, 1) forwards',

            animationDelay:
              '0.05s',

            opacity: 0,

            marginTop:
              '-14px',
          }}
        >

          <h1
            style={{
              fontSize:
                'clamp(2.4rem, 8vw, 5rem)',

              fontWeight: 900,

              margin: 0,

              lineHeight: 1,

              letterSpacing:
                '2px',

              background:
                'linear-gradient(180deg, #eaf9ff 0%, #4fd1ff 55%, #0a6fb0 100%)',

              backgroundSize:
                '100% 200%',

              WebkitBackgroundClip:
                'text',

              WebkitTextFillColor:
                'transparent',

              textShadow:
                '0 0 60px rgba(0, 212, 255, 0.35)',
            }}
          >
            {
              t('homeWelcome')
              || 'TARANG'
            }
          </h1>

          <p
            style={{
              fontSize:
                'clamp(0.95rem, 2vw, 1.15rem)',

              color:
                '#cfe6f5',

              margin:
                '10px 0 0',

              fontWeight:
                400,
            }}
          >
            {t('explorerBrandSub')}
          </p>

          {/* Decorative nav */}

          <div
            style={{
              display:
                'flex',

              alignItems:
                'center',

              justifyContent:
                'center',

              gap:
                '14px',

              marginTop:
                '14px',

              fontSize:
                '12px',

              letterSpacing:
                '3px',

              color:
                '#6fb8d9',

              fontWeight:
                600,
            }}
          >

            {NAV_ITEMS.map(
              (item, i) => (

                <React.Fragment
                  key={item}
                >

                  <span>
                    {item}
                  </span>

                  {i <
                    NAV_ITEMS.length - 1 && (
                    <span
                      style={{
                        color:
                          '#2a5570',
                      }}
                    >
                      •
                    </span>
                  )}

                </React.Fragment>
              )
            )}

          </div>

        </div>

        {/* ───────────────────────────────
            Language selector
        ─────────────────────────────── */}

        <div
          style={{
            display:
              'flex',

            gap:
              '10px',

            flexWrap:
              'wrap',

            justifyContent:
              'center',

            animation:
              'slide-up-stagger 1s cubic-bezier(0.16, 1, 0.3, 1) forwards',

            animationDelay:
              '0.15s',

            opacity:
              0,
          }}
        >

          {LANGUAGES.map(
            lang => (

              <button
                key={lang.code}
                onClick={() =>
                  setLanguage(
                    lang.code
                  )
                }
                style={{
                  background:
                    language === lang.code
                      ? 'rgba(0, 212, 255, 0.18)'
                      : 'rgba(255, 255, 255, 0.03)',

                  border:
                    `1px solid ${
                      language === lang.code
                        ? '#00d4ff'
                        : 'rgba(255, 255, 255, 0.14)'
                    }`,

                  color:
                    language === lang.code
                      ? '#fff'
                      : '#a9cfe0',

                  padding:
                    '9px 20px',

                  borderRadius:
                    '999px',

                  cursor:
                    'pointer',

                  fontSize:
                    '13px',

                  fontWeight:
                    600,

                  display:
                    'flex',

                  alignItems:
                    'center',

                  gap:
                    '6px',

                  transition:
                    'all 0.25s ease',

                  boxShadow:
                    language === lang.code
                      ? '0 0 16px rgba(0, 212, 255, 0.35)'
                      : 'none',

                  backdropFilter:
                    'blur(6px)',
                }}

                onMouseOver={e => {

                  if (
                    language !==
                    lang.code
                  ) {
                    e.currentTarget.style.background =
                      'rgba(255,255,255,0.08)'
                  }

                }}

                onMouseOut={e => {

                  if (
                    language !==
                    lang.code
                  ) {
                    e.currentTarget.style.background =
                      'rgba(255,255,255,0.03)'
                  }

                }}
              >

                {lang.code === 'en' && (
                  <IconGlobe />
                )}

                {lang.nativeLabel}

              </button>

            )
          )}

        </div>

        <button
          onClick={onGetStarted}
          style={{
            padding: '10px 20px',
            border: '1px solid rgba(0, 215, 255, 0.55)',
            borderRadius: '999px',
            background: 'rgba(2, 18, 35, 0.78)',
            color: '#e9fbff',
            fontSize: '12px',
            fontWeight: 700,
            letterSpacing: '1px',
            cursor: 'pointer',
            backdropFilter: 'blur(12px)',
            boxShadow: '0 0 18px rgba(0, 200, 255, 0.15)',
            transition: 'all 0.25s ease',
          }}
          onMouseOver={e => {
            e.currentTarget.style.transform = 'translateY(-2px)'
            e.currentTarget.style.color = '#ffffff'
            e.currentTarget.style.borderColor = '#00ddff'
            e.currentTarget.style.background = 'rgba(0, 45, 70, 0.92)'
            e.currentTarget.style.boxShadow = '0 0 28px rgba(0, 210, 255, 0.35)'
          }}
          onMouseOut={e => {
            e.currentTarget.style.transform = 'translateY(0)'
            e.currentTarget.style.color = '#e9fbff'
            e.currentTarget.style.borderColor = 'rgba(0, 215, 255, 0.55)'
            e.currentTarget.style.background = 'rgba(2, 18, 35, 0.78)'
            e.currentTarget.style.boxShadow = '0 0 18px rgba(0, 200, 255, 0.15)'
          }}
        >
          ✦ GET STARTED
        </button>

        {/* ───────────────────────────────
            Feature cards
        ─────────────────────────────── */}

        <div
          style={{
            display:
              'grid',

            gridTemplateColumns:
              'repeat(auto-fit, minmax(220px, 1fr))',

            gap:
              '14px',

            width:
              '100%',

            marginTop:
              '6px',

            animation:
              'slide-up-stagger 1s cubic-bezier(0.16, 1, 0.3, 1) forwards',

            animationDelay:
              '0.28s',

            opacity:
              0,
          }}
        >

          {[
            {
              icon:
                <IconGlobe />,

              title:
                'Select Region',

              text:
                t('homeStep1'),

              num:
                '01',

              tint:
                '#00d4ff',
            },

            {
              icon:
                <IconLayers />,

              title:
                'Toggle Layers',

              text:
                t('homeStep2'),

              num:
                '02',

              tint:
                '#b06bff',
            },

            {
              icon:
                <IconSliders />,

              title:
                'Change Modes',

              text:
                t('homeStep3'),

              num:
                '03',

              tint:
                '#00e0c0',
            },

          ].map(card => (

            <div
              key={card.num}
              style={{
                background:
                  'rgba(8, 22, 38, 0.55)',

                border:
                  '1px solid rgba(0, 212, 255, 0.18)',

                borderRadius:
                  '14px',

                padding:
                  '20px 18px',

                textAlign:
                  'left',

                backdropFilter:
                  'blur(8px)',

                transition:
                  'all 0.25s ease',
              }}

              onMouseOver={e => {

                e.currentTarget.style.borderColor =
                  card.tint

                e.currentTarget.style.transform =
                  'translateY(-3px)'
              }}

              onMouseOut={e => {

                e.currentTarget.style.borderColor =
                  'rgba(0, 212, 255, 0.18)'

                e.currentTarget.style.transform =
                  'translateY(0)'
              }}
            >

              <div
                style={{
                  display:
                    'flex',

                  alignItems:
                    'center',

                  justifyContent:
                    'space-between',

                  marginBottom:
                    '12px',
                }}
              >

                <span
                  style={{
                    fontSize:
                      '12px',

                    fontWeight:
                      700,

                    color:
                      card.tint,

                    letterSpacing:
                      '1px',
                  }}
                >
                  {card.num}
                </span>

                <div
                  style={{
                    width:
                      42,

                    height:
                      42,

                    borderRadius:
                      '50%',

                    border:
                      `1px solid ${card.tint}55`,

                    background:
                      `${card.tint}14`,

                    display:
                      'flex',

                    alignItems:
                      'center',

                    justifyContent:
                      'center',

                    color:
                      card.tint,
                  }}
                >
                  {card.icon}
                </div>

              </div>

              <div
                style={{
                  fontSize:
                    '15px',

                  fontWeight:
                    700,

                  color:
                    '#f0faff',

                  marginBottom:
                    '6px',

                  borderBottom:
                    `2px solid ${card.tint}44`,

                  paddingBottom:
                    '8px',
                }}
              >
                {card.title}
              </div>

              <div
                style={{
                  fontSize:
                    '12.5px',

                  color:
                    '#a9c6d8',

                  lineHeight:
                    1.5,
                }}
              >
                {card.text}
              </div>

            </div>

          ))}

        </div>

        {/* ───────────────────────────────
            FAQ
        ─────────────────────────────── */}

        <div
          style={{
            width:
              '100%',

            background:
              'rgba(6, 16, 28, 0.55)',

            border:
              '1px solid rgba(0, 212, 255, 0.18)',

            borderRadius:
              '14px',

            padding:
              '16px 18px 8px',

            backdropFilter:
              'blur(8px)',

            textAlign:
              'left',

            marginTop:
              '4px',

            animation:
              'slide-up-stagger 1s cubic-bezier(0.16, 1, 0.3, 1) forwards',

            animationDelay:
              '0.38s',

            opacity:
              0,
          }}
        >

          <div
            style={{
              display:
                'flex',

              alignItems:
                'center',

              gap:
                '8px',

              fontSize:
                '12px',

              letterSpacing:
                '2.5px',

              color:
                '#00d4ff',

              fontWeight:
                700,

              marginBottom:
                '10px',

              textTransform:
                'uppercase',
            }}
          >

            <span
              style={{
                width:
                  18,

                height:
                  18,

                borderRadius:
                  '50%',

                border:
                  '1px solid #00d4ff88',

                display:
                  'flex',

                alignItems:
                  'center',

                justifyContent:
                  'center',

                fontSize:
                  '11px',
              }}
            >
              ?
            </span>

            Why TARANG?

          </div>

          <div
            style={{
              display:
                'flex',

              flexDirection:
                'column',

              gap:
                '6px',

              marginBottom:
                '10px',
            }}
          >

            {FAQ_ITEMS.map(
              (item, idx) => {

                const isOpen =
                  openFaq === idx

                return (

                  <div
                    key={idx}
                    style={{
                      background:
                        isOpen
                          ? 'rgba(0, 212, 255, 0.07)'
                          : 'rgba(255, 255, 255, 0.02)',

                      border:
                        '1px solid rgba(255, 255, 255, 0.08)',

                      borderRadius:
                        '8px',

                      overflow:
                        'hidden',

                      transition:
                        'all 0.25s ease',
                    }}
                  >

                    <button
                      onClick={() =>
                        setOpenFaq(
                          isOpen
                            ? null
                            : idx
                        )
                      }

                      style={{
                        width:
                          '100%',

                        background:
                          'transparent',

                        border:
                          'none',

                        cursor:
                          'pointer',

                        padding:
                          '11px 14px',

                        display:
                          'flex',

                        alignItems:
                          'center',

                        gap:
                          '10px',

                        color:
                          '#e6f4fb',

                        fontSize:
                          '13.5px',

                        fontWeight:
                          500,

                        textAlign:
                          'left',

                        fontFamily:
                          'inherit',
                      }}
                    >

                      <span
                        style={{
                          color:
                            '#00d4ff',

                          fontSize:
                            '12px',

                          transform:
                            isOpen
                              ? 'rotate(90deg)'
                              : 'none',

                          transition:
                            'transform 0.2s ease',

                          flexShrink:
                            0,
                        }}
                      >
                        ▶
                      </span>

                      <span
                        style={{
                          flex:
                            1,
                        }}
                      >
                        {item.q}
                      </span>

                      <span
                        style={{
                          color:
                            '#00d4ff',

                          fontSize:
                            '16px',

                          flexShrink:
                            0,
                        }}
                      >
                        {isOpen
                          ? '−'
                          : '+'}
                      </span>

                    </button>

                    {isOpen && (

                      <div
                        style={{
                          padding:
                            '0 14px 12px 38px',

                          color:
                            '#9fc0d4',

                          fontSize:
                            '12.5px',

                          lineHeight:
                            1.6,
                        }}
                      >
                        {item.a}
                      </div>

                    )}

                  </div>

                )
              }
            )}

          </div>

        </div>

        {/* ───────────────────────────────
            ENTER TARANG
        ─────────────────────────────── */}

        <div
          style={{
            animation:
              'slide-up-stagger 1s cubic-bezier(0.16, 1, 0.3, 1) forwards',

            animationDelay:
              '0.48s',

            opacity:
              0,

            marginTop:
              '6px',
          }}
        >

          <button
            onClick={handleStart}
            style={{
              background:
                'linear-gradient(90deg, #00d4ff 0%, #1046ff 100%)',

              color:
                '#031018',

              border:
                'none',

              padding:
                '15px 40px',

              fontSize:
                '14px',

              fontWeight:
                800,

              letterSpacing:
                '2px',

              textTransform:
                'uppercase',

              borderRadius:
                '999px',

              cursor:
                'pointer',

              display:
                'flex',

              alignItems:
                'center',

              gap:
                '10px',

              boxShadow:
                '0 0 30px rgba(0, 212, 255, 0.45)',

              transition:
                'all 0.25s cubic-bezier(0.16, 1, 0.3, 1)',
            }}

            onMouseOver={e => {

              e.currentTarget.style.boxShadow =
                '0 0 44px rgba(0, 212, 255, 0.7)'

              e.currentTarget.style.transform =
                'scale(1.04)'
            }}

            onMouseOut={e => {

              e.currentTarget.style.boxShadow =
                '0 0 30px rgba(0, 212, 255, 0.45)'

              e.currentTarget.style.transform =
                'scale(1)'
            }}
          >

            {t('homeStartBtn')}

            <span>
              →
            </span>

          </button>

        </div>

        {/* ───────────────────────────────
            Scroll hint
        ─────────────────────────────── */}

        <div
          style={{
            display:
              'flex',

            flexDirection:
              'column',

            alignItems:
              'center',

            gap:
              '4px',

            marginTop:
              '8px',

            color:
              '#6fa8c4',

            fontSize:
              '11px',

            letterSpacing:
              '1px',

            animation:
              'slide-up-stagger 1s cubic-bezier(0.16, 1, 0.3, 1) forwards',

            animationDelay:
              '0.6s',

            opacity:
              0,
          }}
        >


          <IconChevronDown />

        </div>

      </div>

    </div>
  )
}